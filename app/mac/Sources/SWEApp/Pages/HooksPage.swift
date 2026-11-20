import AppKit
import SWECore
import SwiftUI

/// The hook scripts under `~/.swe/hooks` as one table: a row per script per
/// event that fires it, for one harness or every harness, with the scripts
/// nothing fires at the bottom. Clicking a row opens the hook in the table's
/// place, its facts and runs beside the script's editor. Linking goes
/// through `swe init` (the only writer of links) and a new script comes
/// from `swe hooks new`. A harness's settings file is never written here:
/// the new-hook sheet shows the stanza to paste instead.
struct HooksPage: View {
    static let title = "Hooks"
    static let subtitle = "Scripts in ~/.swe/hooks, by the event that fires them in each harness."
    static let backHelp = "Back to all hooks"

    @Environment(AppModel.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var adding = false
    /// The hook open in the table's place, nil while the table shows.
    @State private var opened: Hook?
    /// The harness dropdown's pick; empty, or one that's gone, is every
    /// harness (`HookTable.harness(stored:in:)`).
    @AppStorage("hooksHarness") private var harness = ""
    /// Lay out herdr's, Agent Relay's and other installers' hooks too.
    @AppStorage("hooksShowForeign") private var showForeign = false
    /// The span run counts cover: the range menu, remembered.
    @AppStorage("hooksStatsWindow") private var statsWindow = HookStatsWindow.week
    /// The Check control's findings are folded open under the filter row.
    @State private var showFindings = false
    /// The page panel's width, which picks the open hook's layout; 0
    /// until the first layout.
    @State private var panelWidth: CGFloat = 0

    private var detailLayout: HookDetailLayout {
        HookDetailLayout.of(width: panelWidth > 0 ? HookDetailLayout.detailWidth(panel: panelWidth) : 0)
    }

    /// App-wide, so a list outlives a trip to another page and the last
    /// one shows at once.
    private var model: HooksModel { app.hooks }
    /// Run counts, app-wide for the same reason.
    private var stats: HookStatsModel { app.hookStats }

    private var animation: Animation? {
        reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)
    }

    /// Asks swe for the run counts of the picked window, off the main actor.
    private func loadStats() async {
        await stats.load(window: statsWindow, swe: app.swe, for: app.swePath)
    }

    var body: some View {
        // The open hook's editor fills the window and scrolls itself, so
        // the page scrolls only while the table shows, or when a narrow
        // window stacks the editor under the rail. The switch between
        // layouts follows the window's width as it resizes, unanimated.
        Page(title: Self.title, subtitle: Self.subtitle,
             back: opened == nil ? nil : (Self.backHelp, { app.navReselects += 1 }),
             scrolls: opened == nil || detailLayout == .stacked) {
            Button("New hook", systemImage: "plus") { adding = true }
                .primaryButton()
                .disabled(model.creating)
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await model.load(swe: app.swe, for: app.swePath) }
                Task { await loadStats() }
            }
            .iconButton()
            .disabled(model.linking)
            .keyboardShortcut("r", modifiers: .command)
            .help("Read the hooks and their run counts again (⌘R)")
        } content: {
            if let error = model.actionError { ErrorCard(message: error) }
            if let opened {
                detail(for: opened)
                    .transition(.opacity)
            } else {
                list
                    .transition(.opacity)
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { panelWidth = $0 }
        .task(id: app.swePath) { await model.loadIfStale(swe: app.swe, for: app.swePath) }
        // Counts load with the page, and again when the range menu moves or
        // the swe changes; the Refresh button runs them too. Never on a timer.
        .task(id: [statsWindow.rawValue, app.swePath]) { await loadStats() }
        // The ⌘K search's hook pick opens its detail. Retried on each
        // load: the pick can land before the inventory does.
        .task(id: app.pendingHook) { applyPendingHook() }
        .onChange(of: model.hooks) { _, _ in applyPendingHook() }
        // Modal: a dismiss mid-create would still write the script but
        // lose the stanza the reply carries, so only its buttons close it.
        .cardSheet(isPresented: $adding, modal: true) {
            NewHookSheet()
        }
    }

    @ViewBuilder private var list: some View {
        switch model.state {
        case .idle, .loading:
            SkeletonRows(rows: 6, shape: .hook, label: "Reading hooks…")
        case .failed(let message):
            // An swe from before `hooks` existed fails the whole page with
            // its own words; say what fixes it rather than echoing argv.
            ErrorCard(message: Self.loadMessage(message),
                      actionTitle: "Refresh", actionShortcut: .init("r", modifiers: .command)) {
                Task { await model.load(swe: app.swe, for: app.swePath) }
            }
        case .loaded(let inventory):
            if let checkError = inventory.checkError {
                ErrorCard(message: "The hooks list loaded, but its check didn't run: \(checkError)")
            }
            LinksStrip(hooks: inventory.hooks)
            if inventory.hooks.isEmpty {
                if !inventory.findings.isEmpty {
                    FindingsCard(findings: inventory.findings)
                }
                EmptyCard(text: "No hooks in ~/.swe/hooks yet. A hook script runs when its harness fires an event.",
                          icon: "bolt", actionTitle: "New hook") { adding = true }
            } else {
                HooksOverview(inventory: inventory, counts: stats.state(for: statsWindow),
                              harness: $harness, showForeign: $showForeign,
                              statsWindow: $statsWindow, showFindings: $showFindings,
                              open: open)
            }
        }
    }

    /// The ⌘K search's pick: open its row's detail, and clear the pending
    /// key only once a loaded inventory holds the hook.
    private func applyPendingHook() {
        guard let key = app.pendingHook,
              let hook = model.hooks.first(where: { $0.rowKey == key }) else { return }
        opened = hook
        app.pendingHook = nil
    }

    private func open(_ hook: Hook) {
        withAnimation(animation) { opened = hook }
    }

    /// The open hook in the table's place, wired to the app's models.
    private func detail(for opened: Hook) -> some View {
        // A reload swaps the inventory: follow the same script's row.
        let hook = model.hooks.first { $0.rowKey == opened.rowKey } ?? opened
        let app = app, stats = stats, model = model
        return HookDetailView(
            hook: hook, counts: stats.state(for: statsWindow), window: statsWindow,
            layout: detailLayout,
            home: FileManager.default.homeDirectoryForCurrentUser,
            drafts: model.drafts, findings: model.findings, stats: stats,
            navReselects: app.navReselects,
            info: { name in try? await app.swe.hookInfo(name: name) },
            reloadList: { Task { await model.load(swe: app.swe, for: app.swePath) } },
            loadRecent: { source, window in
                await stats.loadRecent(source: source, window: window, swe: app.swe, for: app.swePath)
            },
            onBack: { withAnimation(animation) { self.opened = nil } })
    }

    /// swe's refusal of a page it does not have reads friendlier said plain.
    private static func loadMessage(_ message: String) -> String {
        message.localizedCaseInsensitiveContains("unknown command")
            ? "This version of swe has no hooks page yet. Update swe, or pick a newer swe in Settings › General."
            : message
    }
}

private extension Hook {
    /// One row of the list: the same script can sit in two harnesses
    /// (herdr's in Claude Code and Codex), so the source alone isn't enough.
    var rowKey: String { "\(harness)|\(id)" }

    /// What's wrong with it, in a badge's words; nil when it's linked and on.
    /// Other installers' scripts have no link of swe's to be wrong.
    var problem: String? {
        if enabled == false { return "Disabled" }
        if tier == .foreign || state == .linked { return nil }
        return state.title
    }
}

/// When any row waits on a link: what `swe init` would do, and the buttons
/// that run it. The same strip the AGENTS.md page carries: init also links
/// the shared skills and instructions, so the copy says so.
private struct LinksStrip: View {
    @Environment(AppModel.self) private var app
    var hooks: [Hook]
    @State private var confirmingReplace = false

    var body: some View {
        let model = app.hooks
        let pending = hooks.filter { $0.state.initFixes }
        let blocked = hooks.filter { $0.state.isBlocked }
        if !pending.isEmpty || !blocked.isEmpty || model.linking {
            Card {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text("Links").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                        Text(pending.isEmpty
                             ? "Every hook script is where its harness reads it."
                             : "\(pending.count) \(pending.count == 1 ? "hook" : "hooks") will be linked into place.")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        if !pending.isEmpty || model.linking {
                            HStack(spacing: 8) {
                                if model.linking { ProgressView().controlSize(.small) }
                                Button(model.linking ? "Linking…" : "Link with swe init", systemImage: "link") {
                                    Task { await model.link(swe: app.swe, for: app.swePath) }
                                }
                                .primaryButton()
                                .disabled(model.linking)
                                .hoverTip("Runs swe init, which also links the shared skills and instructions and never replaces a real file.",
                                          replacesLabel: false)
                            }
                            .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
                        }
                    }
                    if !blocked.isEmpty {
                        HStack(spacing: 12) {
                            Text("A real file is in the way for \(blocked.count == 1 ? "a hook" : "\(blocked.count) hooks"). swe init leaves those alone.")
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Button("Replace and back up…") { confirmingReplace = true }
                                .secondaryButton()
                                // --force also replaces real instruction files
                                // a project edit on the AGENTS.md page may be
                                // writing, so it waits for a clean model there.
                                .disabled(model.linking || app.agentsMd.projectHasUnsavedChanges)
                                .hoverTip(app.agentsMd.projectHasUnsavedChanges ? AgentsModel.saveFirst
                                          : "Runs swe init --force, which copies each real file in the way to ~/.swe/backups before linking over it.",
                                          replacesLabel: false)
                        }
                    }
                }
            }
            .confirmationDialog("Replace the real files with links?", isPresented: $confirmingReplace) {
                Button("Replace and back up", role: .destructive) {
                    Task { await model.link(force: true, swe: app.swe, for: app.swePath) }
                }
            } message: {
                Text("This runs swe init --force. Each real file in the way is copied to ~/.swe/backups, then replaced with a link to the shared copy.")
            }
        }
    }
}

/// The `swe hooks check` report: a declaration naming a script that is
/// gone, a linked script nothing declares, or a script never linked. It
/// opens from the filter row's Check control, which already says "Check".
private struct FindingsCard: View {
    var findings: [HookFinding]

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 0) {
                Text("swe hooks check compares the declarations, the links and the hub.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .padding(.bottom, 4)
                ForEach(Array(findings.enumerated()), id: \.element.id) { index, finding in
                    if index > 0 { Divider().overlay(Theme.border) }
                    HStack(alignment: .top, spacing: 10) {
                        Badge(finding.kindTitle)
                        if !finding.harness.isEmpty {
                            HarnessMarkView(harness: finding.harness, size: 14)
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        }
                        Text(finding.displayMessage(home: FileManager.default.homeDirectoryForCurrentUser.path))
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.vertical, 6)
                }
            }
        }
    }
}

/// The filter row's Check control: a warning mark, the finding count and
/// a chevron, drawn like the menus beside it so the row reads as one.
private struct CheckToggle: View {
    var count: Int
    var isOpen: Bool
    var toggle: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(Theme.warningText)
                Text("Check: \(count) \(count == 1 ? "finding" : "findings")")
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Image(systemName: isOpen ? "chevron.up" : "chevron.down")
                    .font(Theme.font(size: 12))
                    .foregroundStyle(Theme.textSecondary)
            }
            .font(Theme.bodySmall)
            .padding(.horizontal, 12)
            .frame(height: Theme.fieldHeight)
            .background(Theme.field, in: .rect(cornerRadius: Theme.selectRadius))
            .overlay(RoundedRectangle(cornerRadius: Theme.selectRadius)
                .strokeBorder(hovering || isOpen ? Theme.borderHover : Theme.border, lineWidth: 1))
            .contentShape(.rect(cornerRadius: Theme.selectRadius))
        }
        .plainButton(cornerRadius: Theme.selectRadius)
        .onHover { hovering = $0 }
        .fixedSize()
        .help(isOpen ? "Hide what swe hooks check found" : "Show what swe hooks check found")
        .accessibilityValue(isOpen ? "Expanded" : "Collapsed")
    }
}

/// The page under the notices: the filter row, the check's findings when
/// folded open, and the table. It takes everything it shows as values and
/// bindings, so a test can render it from fixture data.
struct HooksOverview: View {
    var inventory: HookInventory
    /// The run counts for the picked window.
    var counts: HookStatsState
    /// `hooksHarness`: empty, or one that's gone, is every harness.
    @Binding var harness: String
    @Binding var showForeign: Bool
    @Binding var statsWindow: HookStatsWindow
    @Binding var showFindings: Bool
    /// The quiet-events line starts open; only a test asks for that.
    var quietEventsOpen = false
    var open: (Hook) -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var animation: Animation? {
        reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)
    }

    var body: some View {
        let hooks = inventory.hooks
        let harnesses = HookTimeline.harnesses(in: hooks)
        let picked = HookTable.harness(stored: harness, in: hooks)
        let table = HookTable(hooks: hooks, harness: picked, includeForeign: showForeign)
        // The labels never wrap: past one row, the checkbox takes a line
        // of its own under the menus, and narrower still the controls
        // take more lines rather than clip.
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                harnessMenu(harnesses, picked: picked)
                rangeMenu
                foreignToggle(table.foreignCount)
                Spacer(minLength: 8)
                checkToggle(inventory.findings)
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    harnessMenu(harnesses, picked: picked)
                    rangeMenu
                    Spacer(minLength: 8)
                    checkToggle(inventory.findings)
                }
                foreignToggle(table.foreignCount)
            }
            // The narrowest windows: the menus on one line, the checkbox
            // and Check on the next, then everything on a line of its own.
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    harnessMenu(harnesses, picked: picked)
                    rangeMenu
                }
                HStack(spacing: 8) {
                    foreignToggle(table.foreignCount)
                    Spacer(minLength: 8)
                    checkToggle(inventory.findings)
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                harnessMenu(harnesses, picked: picked)
                rangeMenu
                foreignToggle(table.foreignCount)
                checkToggle(inventory.findings)
            }
        }
        .modifier(PaneProbe(name: "filters"))
        if showFindings && !inventory.findings.isEmpty {
            FindingsCard(findings: inventory.findings)
                .transition(.opacity.combined(with: .move(edge: .top)))
        }
        if table.rows.isEmpty {
            EmptyCard(text: Self.emptyLine(table), icon: "bolt")
        } else {
            Card {
                VStack(alignment: .leading, spacing: 0) {
                    HooksTable(table: table, counts: counts, open: open)
                    if let picked, !table.quietEvents.isEmpty {
                        Divider().overlay(Theme.border)
                        QuietEvents(harness: picked, events: table.quietEvents, open: quietEventsOpen)
                            .padding(.top, 8)
                    }
                }
            }
        }
    }

    /// Why the table has no rows: only other tools' scripts are declared
    /// and they're hidden, or nothing is declared at all.
    static func emptyLine(_ table: HookTable) -> String {
        let name = table.harness.map { HarnessMark($0).name }
        if table.foreignCount > 0 {
            return name.map { "Only other tools' hooks are declared for \($0). Turn on Other tools to see them." }
                ?? "Only other tools' hooks are declared. Turn on Other tools to see them."
        }
        return name.map { "Nothing is declared for \($0) yet." } ?? "No settings file declares a hook yet."
    }

    /// Every harness first, then each harness with a declared hook, its
    /// logo beside it. Closed, it says "All" so the label stays short.
    private func harnessMenu(_ harnesses: [String], picked: String?) -> some View {
        SelectMenu(label: "Harness",
                   options: [(String?.none, "All harnesses", nil)]
                       + harnesses.map { (String?.some($0), HarnessMark($0).name, $0) },
                   selection: Binding(get: { picked },
                                      set: { pick in withAnimation(animation) { harness = pick ?? "" } }),
                   width: nil,
                   title: picked.map { HarnessMark($0).name } ?? "All")
    }

    private var rangeMenu: some View {
        SelectMenu(options: HookStatsWindow.allCases.map { ($0, $0.title) },
                   selection: $statsWindow, width: nil)
            .help("The span the Fires, Blocks and Rate columns count. They add up every harness that runs the script.")
    }

    @ViewBuilder private func foreignToggle(_ count: Int) -> some View {
        if count > 0 {
            Toggle("Other tools (\(count))", isOn: Binding(
                get: { showForeign },
                set: { on in withAnimation(animation) { showForeign = on } }))
                .toggleStyle(.checkbox)
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .fixedSize()
                .help("Scripts herdr, Agent Relay and other installers declared. SWE lists them but doesn't manage them.")
        }
    }

    /// "Check: 1 finding": folds `swe hooks check`'s findings open under
    /// the filter row. Hidden while the check finds nothing.
    @ViewBuilder private func checkToggle(_ findings: [HookFinding]) -> some View {
        if !findings.isEmpty {
            CheckToggle(count: findings.count, isOpen: showFindings) {
                withAnimation(animation) { showFindings.toggle() }
            }
            .modifier(PaneProbe(name: "check"))
        }
    }
}

// MARK: - Table

/// The table's column widths for the width it has. Matcher and the
/// script's summary give way first, then the Harness column drops its
/// names for logos; past that the table scrolls sideways, like the other
/// tables, rather than squeezing a column.
struct HookColumns: Equatable {
    /// The designed widths, before the text zoom scales them.
    enum Base {
        static let scriptMin: CGFloat = 140
        /// The Script width under which the harness names fold to logos.
        static let scriptComfort: CGFloat = 220
        /// The Script width Matcher waits for before it widens.
        static let scriptIdeal: CGFloat = 280
        static let matcherMin: CGFloat = 68
        static let matcherIdeal: CGFloat = 112
        static let event: CGFloat = 128
        static let harnessFull: CGFloat = 128
        static let harnessCompact: CGFloat = 60
        /// Wide enough for "Blocks" and a five-digit count with both insets.
        static let fires: CGFloat = 60
        static let blocks: CGFloat = 64
        /// The last column pads only its leading edge.
        static let rate: CGFloat = 52
    }

    let script: CGFloat
    let event: CGFloat
    let matcher: CGFloat
    /// 0 when one harness is picked: the column is gone.
    let harness: CGFloat
    /// The Harness column shows logos only.
    let compactHarness: Bool
    let fires: CGFloat
    let blocks: CGFloat
    let rate: CGFloat

    /// Cells pad their text this far from their column edges.
    static let inset: CGFloat = 8
    /// The compact Harness column's smaller inset, so its header fits.
    static let compactInset: CGFloat = 4

    var showsHarness: Bool { harness > 0 }
    /// The fixed columns' separators: one before each column but Script.
    var separators: CGFloat { showsHarness ? 6 : 5 }
    var total: CGFloat { script + event + matcher + harness + fires + blocks + rate + separators }
    /// Everything right of the Script column, for a not-running row's span.
    var afterScript: CGFloat { total - script - 1 }

    /// - Parameter scale: a designed width at the text zoom; tests pass a
    ///   zoom of their own.
    init(available: CGFloat, showsHarness: Bool, scale: (CGFloat) -> CGFloat) {
        let scriptMin = scale(Base.scriptMin)
        let scriptComfort = scale(Base.scriptComfort)
        let scriptIdeal = scale(Base.scriptIdeal)
        let matcherMin = scale(Base.matcherMin)
        let matcherIdeal = scale(Base.matcherIdeal)
        event = scale(Base.event)
        fires = scale(Base.fires)
        blocks = scale(Base.blocks)
        rate = scale(Base.rate)
        let fixed = event + fires + blocks + rate + CGFloat(showsHarness ? 6 : 5)
        let full = scale(Base.harnessFull), compact = scale(Base.harnessCompact)
        if !showsHarness {
            harness = 0
            compactHarness = false
        } else if available - fixed - full >= scriptComfort + matcherMin {
            harness = full
            compactHarness = false
        } else {
            harness = compact
            compactHarness = true
        }
        let flexible = max(available - fixed - harness, scriptMin + matcherMin)
        matcher = min(max(flexible - scriptIdeal, matcherMin), matcherIdeal)
        script = flexible - matcher
    }

    @MainActor init(available: CGFloat, showsHarness: Bool) {
        self.init(available: available, showsHarness: showsHarness, scale: { Theme.scaled($0) })
    }
}

/// A table column, for `HookColumnRecorder`.
enum HookColumn: String, CaseIterable, Sendable {
    case script, event, matcher, harness, fires, blocks, rate
    /// The whole row, where its hover fill draws.
    case row
}

/// Where each header and row cell of the hooks table landed, in the
/// table's own coordinates, keyed by row id ("header" for the header).
/// Only a test sets one; with none in the environment the cells measure
/// nothing.
@MainActor
final class HookColumnRecorder {
    private(set) var frames: [String: [HookColumn: CGRect]] = [:]

    /// The open hook's Runs tiles, by label, in window coordinates.
    private(set) var tiles: [String: CGRect] = [:]

    func record(_ row: String, _ column: HookColumn, _ frame: CGRect) {
        frames[row, default: [:]][column] = frame
    }

    func recordTile(_ label: String, _ frame: CGRect) {
        tiles[label] = frame
    }

    /// The open hook's panes ("detail", "rail", "editor"), in window
    /// coordinates.
    private(set) var panes: [String: CGRect] = [:]

    func recordPane(_ name: String, _ frame: CGRect) {
        panes[name] = frame
    }
}

/// Reports a detail pane's frame to the environment's recorder, if any.
struct PaneProbe: ViewModifier {
    @Environment(\.hookColumnRecorder) private var recorder
    let name: String

    func body(content: Content) -> some View {
        if let recorder {
            content.onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: {
                recorder.recordPane(name, $0)
            }
        } else {
            content
        }
    }
}

/// Reports a Runs tile's frame to the environment's recorder, if any.
private struct TileProbe: ViewModifier {
    @Environment(\.hookColumnRecorder) private var recorder
    let label: String

    func body(content: Content) -> some View {
        if let recorder {
            content.onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: {
                recorder.recordTile(label, $0)
            }
        } else {
            content
        }
    }
}

extension EnvironmentValues {
    @Entry var hookColumnRecorder: HookColumnRecorder? = nil
}

/// Reports a cell's frame to the environment's recorder, if any.
private struct ColumnProbe: ViewModifier {
    @Environment(\.hookColumnRecorder) private var recorder
    let row: String
    let column: HookColumn

    func body(content: Content) -> some View {
        if let recorder {
            content.onGeometryChange(for: CGRect.self) {
                $0.frame(in: .named(HooksTable.space))
            } action: { recorder.record(row, column, $0) }
        } else {
            content
        }
    }
}

private extension View {
    func probe(_ row: String, _ column: HookColumn) -> some View {
        modifier(ColumnProbe(row: row, column: column))
    }
}

/// One row per script per event, in session order, then the scripts
/// nothing fires; the same header, dividers and hover fill as the MCP
/// servers and Skills tables. A row opens its hook.
private struct HooksTable: View {
    /// The coordinate space cells report their frames in.
    static let space = "hooksTable"

    var table: HookTable
    /// The run counts for the picked window, beside every script.
    var counts: HookStatsState
    var open: (Hook) -> Void

    /// Rows the table shows before folding behind a Show more row.
    private static let foldLimit = 20

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showAll = false
    /// The card's inner width: the table fills it, and scrolls only past it.
    @State private var available: CGFloat = 0

    var body: some View {
        let columns = HookColumns(available: available, showsHarness: table.harness == nil)
        ScrollView(.horizontal) {
            VStack(alignment: .leading, spacing: 0) {
                header(columns)
                Divider().overlay(Theme.border)
                let shown = RowFold.visible(table.rows, limit: Self.foldLimit, expanded: showAll)
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, row in
                    HookRow(row: row, columns: columns,
                            cells: counts.cells(for: row.hook.source)) { open(row.hook) }
                    if index < shown.count - 1 { Divider().overlay(Theme.border) }
                }
                let hidden = RowFold.hiddenCount(total: table.rows.count, limit: Self.foldLimit)
                if hidden > 0 {
                    Divider().overlay(Theme.border)
                    ExpandRow(isExpanded: showAll, hidden: hidden,
                              help: (expand: "Show every row",
                                     collapse: "Show only the first \(Self.foldLimit)"),
                              edge: .inset) {
                        withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) {
                            showAll.toggle()
                        }
                    }
                }
            }
            // Exactly the card's width, or the columns' minimum if wider, so
            // a long name truncates instead of pushing the numbers away.
            .frame(width: max(available, columns.total), alignment: .leading)
            .coordinateSpace(name: Self.space)
        }
        .scrollIndicators(.automatic)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { available = $0 }
    }

    /// The column names. Each separator reaches through the row's bottom
    /// padding to the divider, so the column edges read as one line.
    private func header(_ columns: HookColumns) -> some View {
        let inset = HookColumns.inset
        return HStack(spacing: 0) {
            Text("Script")
                .padding(.trailing, inset)
                .frame(width: columns.script, alignment: .leading)
                .probe("header", .script)
            columnSeparator().padding(.bottom, -8)
            Text("Event")
                .padding(.horizontal, inset)
                .frame(width: columns.event, alignment: .leading)
                .probe("header", .event)
            columnSeparator().padding(.bottom, -8)
            Text("Matcher")
                .padding(.horizontal, inset)
                .frame(width: columns.matcher, alignment: .leading)
                .probe("header", .matcher)
            if columns.showsHarness {
                columnSeparator().padding(.bottom, -8)
                Text("Harness")
                    .padding(.horizontal, columns.compactHarness ? HookColumns.compactInset : inset)
                    .frame(width: columns.harness,
                           alignment: columns.compactHarness ? .center : .leading)
                    .probe("header", .harness)
            }
            columnSeparator().padding(.bottom, -8)
            Text("Fires")
                .padding(.horizontal, inset)
                .frame(width: columns.fires, alignment: .trailing)
                .probe("header", .fires)
            columnSeparator().padding(.bottom, -8)
            Text("Blocks")
                .padding(.horizontal, inset)
                .frame(width: columns.blocks, alignment: .trailing)
                .probe("header", .blocks)
            columnSeparator().padding(.bottom, -8)
            Text("Rate")
                .padding(.leading, inset)
                .frame(width: columns.rate, alignment: .trailing)
                .probe("header", .rate)
        }
        .frame(width: columns.total, alignment: .leading)
        .lineLimit(1)
        .font(Theme.tableHeader)
        .tracking(0.12)
        .foregroundStyle(Theme.tableHeaderText)
        .padding(.bottom, 8)
    }
}

/// One row: a script on one event with its matcher, harness and run
/// counts, or a script nothing fires, muted, its reason across the rest.
/// Another installer's script reads quieter, behind a puzzle-piece mark.
private struct HookRow: View {
    var row: HookTable.Row
    var columns: HookColumns
    var cells: HookStatsCells
    var open: () -> Void
    @State private var hovering = false

    private var hook: Hook { row.hook }

    var body: some View {
        Button(action: open) {
            // Every cell is a `cell(...)`, which always takes its column's
            // width: an empty Matcher must never pull the numbers left.
            HStack(spacing: 0) {
                cell(.script, width: columns.script, leading: 0) { scriptCell }
                columnSeparator()
                if let event = row.event {
                    cell(.event, width: columns.event) { eventCell(event) }
                    columnSeparator()
                    cell(.matcher, width: columns.matcher) { matcherCell }
                    if columns.showsHarness {
                        columnSeparator()
                        cell(.harness, width: columns.harness,
                             leading: columns.compactHarness ? HookColumns.compactInset : HookColumns.inset,
                             trailing: columns.compactHarness ? HookColumns.compactInset : HookColumns.inset,
                             alignment: columns.compactHarness ? .center : .leading) { harnessCell }
                    }
                    StatCells(cells: cells, columns: columns, row: row.id)
                } else {
                    cell(.event, width: columns.afterScript, trailing: 0) { notRunningCell }
                }
            }
            .frame(width: columns.total, alignment: .leading)
            .padding(.vertical, 8)
            .background(hovering ? Theme.rowHover : .clear)
            .contentShape(.rect)
            .probe(row.id, .row)
        }
        .plainButton(cornerRadius: Theme.smallRadius)
        .onHover { hovering = $0 }
        .help(hook.summary.isEmpty ? "Open \(hook.script)" : hook.summary)
        .accessibilityLabel(Text(spoken))
    }

    /// One column's cell: its content inset from the column edges, over a
    /// zero-height spacer so the cell keeps the column's width even when
    /// the content is empty.
    private func cell<Content: View>(_ column: HookColumn, width: CGFloat,
                                     leading: CGFloat = HookColumns.inset,
                                     trailing: CGFloat = HookColumns.inset,
                                     alignment: Alignment = .leading,
                                     @ViewBuilder _ content: () -> Content) -> some View {
        ZStack(alignment: alignment) {
            Color.clear.frame(height: 0)
            content()
        }
        .padding(.leading, leading)
        .padding(.trailing, trailing)
        .frame(width: width, alignment: alignment)
        .probe(row.id, column)
    }

    /// The name, anything wrong with it, and its header line under it.
    private var scriptCell: some View {
        // A not-running row's reason cell already shows the summary when
        // swe has no note of its own, so it shows once.
        let summary = row.isRunning || !hook.detail.isEmpty ? HookTimeline.shortSummary(hook.summary) : ""
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                if row.isForeign {
                    Image(systemName: "puzzlepiece.extension")
                        .font(Theme.font(size: 11))
                        .foregroundStyle(Theme.textMuted)
                        .hoverTip("Declared by another tool")
                }
                Text(hook.script)
                    .font(Theme.font(size: 13, weight: .medium, design: .monospaced))
                    .foregroundStyle(row.isForeign || !row.isRunning ? Theme.textSecondary : Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                    .layoutPriority(1)
                // A not-running row's badge sits in its wide reason cell.
                if row.isRunning, let problem = hook.problem {
                    Badge(problem)
                }
            }
            if !summary.isEmpty {
                Text(summary)
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .lineLimit(1).truncationMode(.tail)
            }
        }
    }

    /// The event's name and when it fires.
    private func eventCell(_ event: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(event)
                .font(Theme.font(size: 12, design: .monospaced))
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(1).truncationMode(.tail)
            if let caption = HookEvents.caption(for: event) {
                Text(caption)
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .lineLimit(1).truncationMode(.tail)
            }
        }
    }

    @ViewBuilder private var matcherCell: some View {
        if let label = row.matcherLabel {
            MatcherPill(text: label).help(label)
        }
    }

    /// The harness's logo and name; the logo alone when narrow.
    @ViewBuilder private var harnessCell: some View {
        let mark = HarnessMark(hook.harness)
        HStack(spacing: 6) {
            if let icon = mark.icon { HarnessIcon(harness: icon, size: 16) }
            if !columns.compactHarness || mark.icon == nil {
                Text(mark.name)
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                    .lineLimit(1).truncationMode(.tail)
            }
        }
        .help(mark.name)
    }

    /// "Not running" and why, across every column right of Script.
    private var notRunningCell: some View {
        HStack(spacing: 8) {
            // Every harness's rows say whose folder it is; a folder that is
            // no harness has no mark to show.
            if columns.showsHarness, let icon = HarnessMark(hook.harness).icon {
                HarnessIcon(harness: icon, size: 14)
                    .help(HarnessMark(hook.harness).name)
            }
            Text("Not running")
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .lineLimit(1).fixedSize()
            // "Not installed" is skipped's word for a missing harness; the
            // reason beside it says the real one.
            if let problem = hook.problem, hook.state != .skipped {
                Badge(problem).fixedSize()
            }
            Text(Self.reason(hook))
                .font(Theme.caption).foregroundStyle(Theme.textMuted)
                .lineLimit(1).truncationMode(.tail)
        }
    }

    /// swe's note on the row, else the script's header line, else the plain fact.
    static func reason(_ hook: Hook) -> String {
        if !hook.detail.isEmpty { return hook.detail.prefix(1).uppercased() + hook.detail.dropFirst() }
        let summary = HookTimeline.shortSummary(hook.summary)
        return summary.isEmpty ? "No settings file declares it" : summary
    }

    private var spoken: String {
        var parts = [hook.script]
        if row.isForeign { parts.append("declared by another tool") }
        if let problem = hook.problem { parts.append(problem) }
        if let event = row.event {
            parts.append("on \(event)")
            if let label = row.matcherLabel { parts.append(label) }
            if columns.showsHarness { parts.append(HarnessMark(hook.harness).name) }
            parts.append(StatCells.spoken(cells))
        } else {
            parts.append("not running, \(Self.reason(hook))")
        }
        return parts.joined(separator: ", ")
    }
}

/// One script's fires, blocks and block rate, each behind its column's
/// separator. Bars stand in while swe reads, dashes when it could not
/// answer, and the numbers fade in (unless Reduce Motion is on).
private struct StatCells: View {
    var cells: HookStatsCells
    var columns: HookColumns
    /// The row's id, for `HookColumnRecorder`.
    var row: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 0) {
            columnSeparator()
            cell(fires, bar: 20).padding(.horizontal, HookColumns.inset)
                .frame(width: columns.fires, alignment: .trailing)
                .probe(row, .fires)
            columnSeparator()
            cell(blocks, bar: 20).padding(.horizontal, HookColumns.inset)
                .frame(width: columns.blocks, alignment: .trailing)
                .probe(row, .blocks)
            columnSeparator()
            cell(rate, bar: 26).padding(.leading, HookColumns.inset)
                .frame(width: columns.rate, alignment: .trailing)
                .probe(row, .rate)
        }
        .animation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration), value: cells)
    }

    private typealias Figure = (text: String, color: Color)?

    /// nil while loading: the cell draws a bar.
    private var fires: Figure {
        switch cells {
        case .loading: nil
        case .dashes: ("-", Theme.textMuted)
        case .counts(let c): (c.fires.formatted(), c.fires > 0 ? Theme.textPrimary : Theme.textMuted)
        }
    }

    private var blocks: Figure {
        switch cells {
        case .loading: nil
        case .dashes: ("-", Theme.textMuted)
        case .counts(let c): (c.blocks.formatted(), c.blocks > 0 ? Theme.dangerText : Theme.textMuted)
        }
    }

    private var rate: Figure {
        switch cells {
        case .loading: nil
        case .dashes: ("-", Theme.textMuted)
        case .counts(let c): (c.rateLabel, c.blocks > 0 ? Theme.textPrimary : Theme.textMuted)
        }
    }

    /// What VoiceOver reads for the three numbers.
    static func spoken(_ cells: HookStatsCells) -> String {
        switch cells {
        case .loading: "counting runs"
        case .dashes: "run counts unavailable"
        case .counts(let c):
            c.hasRuns ? "\(c.fires) runs, \(c.blocks) blocked, block rate \(c.rateLabel)" : "no runs"
        }
    }

    private func cell(_ figure: Figure, bar: CGFloat) -> some View {
        ZStack(alignment: .trailing) {
            if let figure {
                Text(figure.text)
                    .font(Theme.font(size: 13))
                    .monospacedDigit()
                    .foregroundStyle(figure.color)
                    .lineLimit(1)
                    .id(figure.text)
                    .transition(.opacity)
            } else {
                SkeletonBlock(width: bar, height: 10, radius: 3)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, minHeight: Theme.scaled(16), alignment: .trailing)
    }
}

/// The tool or pattern a declaration is gated to, tinted so it reads as
/// the one thing that decides whether the script runs.
private struct MatcherPill: View {
    var text: String

    var body: some View {
        Text(text)
            .font(Theme.font(size: 11, weight: .medium, design: .monospaced))
            .foregroundStyle(Theme.link)
            .lineLimit(1).truncationMode(.tail)
            .padding(.horizontal, 7)
            .padding(.vertical, 1)
            .background(Theme.link.opacity(0.12), in: .capsule)
    }
}

/// The events one harness supports that nothing shown is declared on: a
/// muted line under the table that opens in place, in session order.
private struct QuietEvents: View {
    var harness: String
    var events: [String]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var open: Bool

    init(harness: String, events: [String], open: Bool = false) {
        self.harness = harness
        self.events = events
        _open = State(initialValue: open)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration)) { open.toggle() }
            } label: {
                Label(open ? "Hide events with nothing on them"
                           : "\(events.count) more \(events.count == 1 ? "event has" : "events have") nothing on them",
                      systemImage: open ? "chevron.up" : "chevron.down")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .padding(.vertical, 4)
                    .contentShape(.rect)
            }
            .plainButton(cornerRadius: Theme.smallRadius)
            .help("Events \(HarnessMark(harness).name) fires that no script shown here runs on")
            if open {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(events, id: \.self) { event in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(event)
                                .font(Theme.font(size: 12, design: .monospaced))
                                .foregroundStyle(Theme.textSecondary)
                                .lineLimit(1)
                                .frame(width: Theme.scaled(160), alignment: .leading)
                            if let caption = HookEvents.caption(for: event) {
                                Text(caption).font(Theme.caption).foregroundStyle(Theme.textMuted)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
                .padding(.bottom, 4)
                .transition(.opacity)
            }
        }
    }
}

// MARK: - Hook detail

/// How the open hook lays out for the width `Page` leaves it: the rail
/// beside the editor at 300pt, then 260pt, and below that the two stacked
/// at full width, since the editor would drop under `editorMin`.
enum HookDetailLayout: Equatable {
    case wide
    case narrowRail
    case stacked

    static let railWidth: CGFloat = 300
    static let narrowRailWidth: CGFloat = 260
    static let gap: CGFloat = 12
    /// The narrowest the editor pane may get beside the rail.
    static let editorMin: CGFloat = 380
    /// The editor's height when stacked under the rail, where the page
    /// scrolls and nothing else gives it one.
    static let stackedEditorMin: CGFloat = 360
    static let stackedEditorIdeal: CGFloat = 440
    /// Stacked, Declaration and Runs sit side by side from this width.
    static let sideBySideFrom: CGFloat = 580
    /// `Page`'s side padding, 40 a side.
    static let pagePadding: CGFloat = 80

    /// The detail's width in a page panel `panel` wide (`Page` centres a
    /// 1280pt column inside its padding).
    static func detailWidth(panel: CGFloat) -> CGFloat {
        min(panel, 1280) - pagePadding
    }

    /// The narrowest detail the app can show: the minimum window with the
    /// sidebar dragged to its widest.
    static func narrowest(window: CGFloat = 900) -> CGFloat {
        detailWidth(panel: window - CGFloat(SidebarWidth.range.upperBound))
    }

    /// What a scrolling page's scroll bar takes from its content: nothing
    /// with overlay scroll bars (a trackpad), its width when System
    /// Settings shows them always (a mouse, or a CI runner).
    @MainActor static func scrollerAllowance(_ style: NSScroller.Style) -> CGFloat {
        style == .legacy ? NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy) : 0
    }

    /// The width the detail is laid out at. Only a stacked detail makes the
    /// page scroll, so only it loses a legacy scroll bar's width; the two
    /// side-by-side layouts get the page's whole width, which is why `of`
    /// can decide from `detailWidth` alone and the editor's 380pt holds
    /// under either scroller style.
    @MainActor static func laidOutWidth(panel: CGFloat, style: NSScroller.Style) -> CGFloat {
        let width = detailWidth(panel: panel)
        return of(width: width) == .stacked ? width - scrollerAllowance(style) : width
    }

    /// The layout for a detail `width` wide (the page's width, before any
    /// scroll bar); 0 (not measured yet) is wide.
    static func of(width: CGFloat) -> HookDetailLayout {
        guard width > 0 else { return .wide }
        if width - railWidth - gap >= editorMin { return .wide }
        if width - narrowRailWidth - gap >= editorMin { return .narrowRail }
        return .stacked
    }
}

/// One hook in the table's place: a rail with what fires it, where it's
/// declared and linked, and how it ran, beside the script in a raw editor
/// that fills the window. The script is the hub copy under ~/.swe/hooks;
/// edits to it reach every link `swe init` made. InstructionDocument
/// preserves the file's executable bit across a save. Everything it
/// reads from the app comes in as a value or a closure, so a test can
/// render it from fixtures without spawning swe.
struct HookDetailView: View {
    /// What the first look at the script found: its ownership and whether
    /// it is a folder.
    struct Checked {
        var check: HookScript
        var isFolder: Bool
    }

    var hook: Hook
    /// The run counts for `window`, from the page.
    var counts: HookStatsState
    var window: HookStatsWindow
    /// Rail beside the editor, or the two stacked; the page decides from
    /// its own width, since a stacked detail also makes the page scroll.
    var layout: HookDetailLayout
    /// The home whose `~/.swe/hooks` decides what is editable.
    var home: URL
    /// The model's, not the view's: going back keeps the drafts so an
    /// edit survives a trip to another hook and the app's quit check
    /// still sees it.
    var drafts: PrimitiveDrafts
    /// `swe hooks check`'s findings, for the managed-file lock.
    var findings: [HookFinding]
    var stats: HookStatsModel
    /// `AppModel.navReselects`: the header's back chevron and a sidebar
    /// reselect bump it.
    var navReselects: Int
    /// `swe hooks info <name>`, nil when it fails.
    var info: (String) async -> HookInfo?
    /// Reads the hooks list again after a save.
    var reloadList: () -> Void
    /// `swe hooks log` for the Runs group's recent runs.
    var loadRecent: (String, HookStatsWindow) async -> Void
    var onBack: () -> Void

    init(hook: Hook, counts: HookStatsState, window: HookStatsWindow, layout: HookDetailLayout, home: URL,
         drafts: PrimitiveDrafts, findings: [HookFinding], stats: HookStatsModel,
         navReselects: Int,
         info: @escaping (String) async -> HookInfo?,
         reloadList: @escaping () -> Void,
         loadRecent: @escaping (String, HookStatsWindow) async -> Void,
         onBack: @escaping () -> Void,
         checked: Checked? = nil) {
        self.hook = hook
        self.counts = counts
        self.window = window
        self.layout = layout
        self.home = home
        self.drafts = drafts
        self.findings = findings
        self.stats = stats
        self.navReselects = navReselects
        self.info = info
        self.reloadList = reloadList
        self.loadRecent = loadRecent
        self.onBack = onBack
        // A test hands the first look in, so the first frame shows it.
        _found = State(initialValue: checked.map { (hook.source, $0.check) })
        _isFolder = State(initialValue: checked?.isFolder ?? false)
    }

    /// The model's, not the view's: going back keeps the drafts so an
    /// edit survives a trip to another hook and the app's quit check
    /// still sees it.
    @State private var leaving = false
    @State private var leaveError: String?
    /// The stacked rail's width, for Declaration beside Runs; 0 until
    /// the first layout.
    @State private var width: CGFloat = 0
    /// The script's ownership, worked out off the main actor, with the
    /// path it was worked out for; nil until the first lands.
    @State private var found: (path: String, check: HookScript)?
    /// `swe hooks info`'s take on the script, for the command line and
    /// timeout the list leaves out, with the source it was asked for, so
    /// a reply for another hook never shows under this one's name.
    @State private var fetched: (source: String, info: HookInfo)?
    /// Bumped by `reload()` so the check is worked out again after a save.
    @State private var refresh = 0
    /// A directory hook (a bundle of scripts) has no one file to edit.
    @State private var isFolder = false

    private var source: URL { URL(fileURLWithPath: hook.source) }
    /// The check for the hook on screen; nil while it is worked out, so a
    /// result for a hook opened before never shows.
    private var check: HookScript? {
        guard let found, found.path == hook.source else { return nil }
        return found.check
    }
    /// The placement swe's info call reports for this harness.
    private var placement: HookInfo.Placement? {
        guard let fetched, fetched.source == hook.source else { return nil }
        return fetched.info.harnesses.first { $0.harness == hook.harness }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let leaveError {
                ErrorCard(message: leaveError)
            }
            switch layout {
            case .wide, .narrowRail:
                HStack(alignment: .top, spacing: HookDetailLayout.gap) {
                    rail
                        .frame(width: layout == .wide ? HookDetailLayout.railWidth : HookDetailLayout.narrowRailWidth)
                        .frame(maxHeight: .infinity)
                        .modifier(PaneProbe(name: "rail"))
                    scriptPane
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                        .modifier(PaneProbe(name: "editor"))
                }
                .frame(maxHeight: .infinity)
            case .stacked:
                // The page scrolls: the rail at full width on top, then
                // the editor at a height of its own.
                stackedRail
                    .modifier(PaneProbe(name: "rail"))
                scriptPane
                    .frame(minHeight: HookDetailLayout.stackedEditorMin,
                           idealHeight: HookDetailLayout.stackedEditorIdeal, alignment: .top)
                    .modifier(PaneProbe(name: "editor"))
            }
        }
        .frame(maxHeight: layout == .stacked ? nil : .infinity, alignment: .top)
        .modifier(PaneProbe(name: "detail"))
        .task(id: [hook.source, home.path, String(refresh)]) {
            let source = hook.source, home = home
            let (check, folder) = await offMain {
                var directory: ObjCBool = false
                let folder = FileManager.default.fileExists(atPath: source, isDirectory: &directory)
                    && directory.boolValue
                return (HookScript(source: source, home: home), folder)
            }
            guard !Task.isCancelled else { return }
            // Open the document here, never in body: document(for:) writes
            // the drafts store, which body must not do mid-update.
            if check.exists && !folder { drafts.document(for: source) }
            isFolder = folder
            found = (source, check)
        }
        // Refetch on a refreshed row or a save: the row the page hands
        // down may differ from what the first look fetched.
        .task(id: InfoKey(hook: hook, refresh: refresh)) {
            let source = hook.source
            let answer = await info(hook.script)
            guard !Task.isCancelled else { return }
            fetched = answer.map { (source, $0) }
        }
        // The page header's back chevron (and its ⌘[) and reselecting the
        // Hooks sidebar item both come through here.
        .onChange(of: navReselects) { leave() }
        .alert("Unsaved changes", isPresented: $leaving) {
            Button("Save") { saveAndLeave() }
            // Discard drops the document, so reopening reads disk, not
            // the edit that was thrown away.
            Button("Discard", role: .destructive) {
                drafts.remove(under: hook.source)
                onBack()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("“\(hook.script)” has unsaved changes.")
        }
    }

    /// Re-asks `swe hooks info` when the hook row changes under the open
    /// detail (a Refresh) or after a save (`reload` bumps `refresh`).
    private struct InfoKey: Equatable {
        var hook: Hook
        var refresh: Int
    }

    /// The left rail: the script and its summary, its declaration, and its
    /// runs. It scrolls on its own when it is taller than the window.
    private var rail: some View {
        Strip {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    identity
                    Divider().overlay(Theme.border)
                    declaration
                    HookRunsGroup(source: hook.source, counts: counts, window: window,
                                  stats: stats, loadRecent: loadRecent)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.trailing, 4)
            }
            .scrollIndicators(.automatic)
            .frame(maxHeight: .infinity)
        }
        .frame(maxHeight: .infinity)
    }

    /// The rail above the editor in a narrow window: no scroll of its own
    /// (the page scrolls), and Declaration beside Runs when there's room
    /// for both, so it doesn't become one tall column.
    private var stackedRail: some View {
        Strip {
            VStack(alignment: .leading, spacing: 16) {
                identity
                Divider().overlay(Theme.border)
                if width >= HookDetailLayout.sideBySideFrom {
                    HStack(alignment: .top, spacing: 24) {
                        declaration.frame(maxWidth: .infinity, alignment: .topLeading)
                        HookRunsGroup(source: hook.source, counts: counts, window: window,
                                      stats: stats, loadRecent: loadRecent, divider: false)
                            .frame(maxWidth: .infinity, alignment: .topLeading)
                    }
                } else {
                    declaration
                    HookRunsGroup(source: hook.source, counts: counts, window: window,
                                  stats: stats, loadRecent: loadRecent)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }

    /// The right pane: the lock note over a read-only script, then the
    /// editor, or why there is none.
    @ViewBuilder private var scriptPane: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let check, !check.editable {
                PrimitiveLockNote(text: "Only scripts inside ~/.swe/hooks can be edited, so \(hook.script) is read-only.")
            }
            if let check {
                if !check.exists {
                    Card {
                        Text("The script is missing on disk; swe init may have removed it.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    }
                } else if isFolder {
                    Card {
                        Text("\(hook.script) is a folder, so there's no single script to show. Show it in Finder to see what's inside.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else if let document = drafts.documents[hook.source] {
                    InstructionEditor(
                        document: document,
                        title: hook.script,
                        subtitle: shown(source.path),
                        monoSubtitle: true,
                        readOnly: !check.editable,
                        rawOnly: true,
                        onSaved: reload)
                        .frame(maxHeight: .infinity)
                } else {
                    SkeletonLines(rows: 10, label: "Opening the script…")
                }
            } else {
                SkeletonDetail(label: "Checking the script…")
            }
        }
    }

    /// The script's name, its header line and Show in Finder.
    private var identity: some View {
        let summary = [placement?.summary ?? "", hook.summary].first { !$0.isEmpty } ?? ""
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 6) {
                Text(hook.script)
                    .font(Theme.font(size: 16, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                    .help(hook.script)
                Spacer(minLength: 8)
                Button { NSWorkspace.shared.activateFileViewerSelecting([source]) } label: {
                    Image(systemName: "folder")
                }
                .iconButton()
                .help("Show in Finder")
                .accessibilityLabel("Show in Finder")
            }
            if !summary.isEmpty {
                Text(summary).font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Where the script is declared and linked: the harness, the events
    /// it runs on, the command, the settings files, the link.
    private var declaration: some View {
        let declared = placement?.declared ?? hook.declared
        let settings = declared.map(\.settings).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        let others = (hook.eventsSupported ?? []).filter { event in !declared.contains { $0.event == event } }
        return VStack(alignment: .leading, spacing: 10) {
            Text("Declaration").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
            VStack(alignment: .leading, spacing: 8) {
                InfoLine("Harness") {
                    FlowLayout(spacing: 6, lineSpacing: 6) {
                        HStack(spacing: 6) {
                            if let icon = HarnessMark(hook.harness).icon { HarnessIcon(harness: icon, size: 14) }
                            Text(HarnessMark(hook.harness).name)
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        }
                        // "Harness" is every row's tier but two; only those say so.
                        if let tier = hook.tier, tier != .harness { Badge(tier.title) }
                        if let problem = hook.problem { Badge(problem) }
                    }
                }
                if declared.isEmpty {
                    InfoLine("Runs") {
                        Text("Never: no settings file declares it")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    InfoLine("Runs") {
                        FlowLayout(spacing: 6, lineSpacing: 6) {
                            ForEach(Array(declared.enumerated()), id: \.offset) { _, declaration in
                                HStack(spacing: 4) {
                                    Badge(declaration.label)
                                    if let timeout = declaration.timeout {
                                        Text("\(timeout.formatted())s")
                                            .font(Theme.caption).foregroundStyle(Theme.textMuted)
                                            .help("Timeout")
                                    }
                                }
                            }
                        }
                    }
                    if let command = declared.first(where: { !$0.command.isEmpty })?.command {
                        InfoLine("Command") { mono(command) }
                    }
                    InfoLine("Declared in") {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(settings, id: \.self) { path in
                                HStack(spacing: 4) {
                                    mono(shown(path))
                                    if isManaged(path) {
                                        Image(systemName: "lock")
                                            .font(Theme.font(size: 10))
                                            .foregroundStyle(Theme.textMuted)
                                            .hoverTip("Another tool owns this file; SWE never writes it.")
                                            .help("Another tool owns this file; SWE never writes it.")
                                    }
                                }
                            }
                        }
                    }
                }
                InfoLine("Linked at") { mono(shown(hook.destination)) }
                if !hook.detail.isEmpty {
                    InfoLine("Note") {
                        Text(hook.detail).font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if !others.isEmpty {
                    InfoLine("Can also run") {
                        Text("\(others.count) other \(others.count == 1 ? "event" : "events")")
                            .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                            .hoverTip(others.joined(separator: ", "), title: "Events this script handles")
                            .help(others.joined(separator: ", "))
                    }
                }
            }
        }
    }

    /// A path under `home` as `~/…`, the way `displayPath` writes paths
    /// under the account's home.
    private func shown(_ path: String) -> String {
        let root = home.path.hasSuffix("/") ? String(home.path.dropLast()) : home.path
        return path.hasPrefix(root + "/") ? "~" + path.dropFirst(root.count) : displayPath(path)
    }

    private func mono(_ text: String) -> some View {
        Text(text)
            .font(Theme.font(size: 12, design: .monospaced))
            .foregroundStyle(Theme.textSecondary)
            .lineLimit(1).truncationMode(.middle)
            .help(text)
    }

    /// `swe hooks check` notes a settings file another tool owns (nix,
    /// dotfiles) as `managed`, its path first.
    private func isManaged(_ path: String) -> Bool {
        findings.contains { $0.kind == "managed" && $0.message.hasPrefix(path) }
    }

    /// This hook's own draft. Another hook's edit stays in the drafts
    /// store when this one opens: the app's quit check still sees it, and
    /// reopening that hook brings it back. So going back asks about,
    /// saves or discards this script only.
    private var draft: InstructionDocument? {
        drafts.documents[hook.source].flatMap { $0.isDirty ? $0 : nil }
    }

    /// Back to the table with nothing unsaved here; otherwise ask first.
    private func leave() {
        if draft == nil { onBack() } else { leaving = true }
    }

    /// Save the script, then go back; a save that fails keeps the detail
    /// open and says why.
    private func saveAndLeave() {
        Task {
            if let draft, await draft.save() != .saved || draft.isDirty {
                leaveError = "Couldn't save \(hook.script)."
            } else {
                leaveError = nil
                onBack()
            }
        }
    }

    private func reload() {
        refresh += 1
        reloadList()
    }
}

/// How the open hook did in the window: fires, blocks and the last block,
/// why it blocked and its newest runs. Nothing shows while swe cannot
/// answer; the rail is whole without it.
private struct HookRunsGroup: View {
    /// The hook's source, which the counts are keyed by.
    var source: String
    var counts: HookStatsState
    var window: HookStatsWindow
    var stats: HookStatsModel
    var loadRecent: (String, HookStatsWindow) async -> Void
    /// The rule above "Runs"; off beside Declaration in a stacked rail.
    var divider = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let recentLimit = 5

    /// Whether the group lists recent runs: swe answered, knows which runs
    /// are this script's, and it ran.
    private var wantsRecent: Bool {
        guard case .ready(let table) = counts else { return false }
        return !table.isUnknown(source) && table.counts(for: source).hasRuns
    }

    var body: some View {
        Group {
            switch counts {
            case .unavailable:
                EmptyView()
            case .loading:
                section { loadingBody }
            case .ready(let table):
                let summed = table.counts(for: source)
                section {
                    if !table.identified {
                        Text("This swe is too old to say which runs belong to this script.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if table.isUnknown(source) {
                        Text("swe can't tell which runs belong to this script.")
                            .font(Theme.bodySmall).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if summed.hasRuns { readyBody(summed) } else {
                        Text(window.emptyLine)
                            .font(Theme.bodySmall).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: Theme.fadeDuration), value: counts)
        // The log is asked for again whenever the counts are: a new window
        // or a refresh.
        .task(id: RecentKey(source: source, wanted: wantsRecent, window: window,
                            answered: stats.completed)) {
            guard wantsRecent else { return }
            await loadRecent(source, window)
        }
    }

    private struct RecentKey: Equatable {
        var source: String
        var wanted: Bool
        var window: HookStatsWindow
        var answered: Int
    }

    /// The rail's divider, "Runs" and the range it covers, then `content`.
    private func section<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if divider { Divider().overlay(Theme.border) }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Runs").font(Theme.cardTitle).foregroundStyle(Theme.textPrimary)
                Text(window.title).font(Theme.caption).foregroundStyle(Theme.textSecondary)
            }
            content()
        }
    }

    /// Bars shaped like the tiles and lines that follow.
    private var loadingBody: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                ForEach(0..<3, id: \.self) { _ in
                    SkeletonBlock(width: nil, height: 52, radius: Theme.smallRadius)
                }
            }
            ForEach(0..<2, id: \.self) { _ in SkeletonBlock(width: nil, height: 12, radius: 3) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("Counting runs"))
    }

    private func readyBody(_ summed: HookCounts) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            let fires = tile("Fires", summed.fires.formatted(),
                             color: summed.fires > 0 ? Theme.textPrimary : Theme.textMuted)
            let blocks = tile("Blocks", summed.blocks.formatted(),
                              color: summed.blocks > 0 ? Theme.dangerText : Theme.textMuted)
            let last = tile("Last block", summed.lastBlock.map { HookStatsFormat.relative($0) } ?? "-",
                            color: summed.lastBlock == nil ? Theme.textMuted : Theme.textPrimary)
            // Three across when every value fits whole; otherwise Last
            // block takes its own row under the other two. The tiles never
            // shrink their text, so all three stay the same size.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { fires; blocks; last }
                VStack(spacing: 8) {
                    HStack(spacing: 8) { fires; blocks }
                    last
                }
            }
            if !summed.topReasons.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    sectionLabel("Top reasons")
                    ForEach(summed.topReasons) { reason in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(reason.reason.isEmpty ? "No reason given" : reason.reason)
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                .lineLimit(2)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(reason.countLabel)
                                .font(Theme.caption)
                                .foregroundStyle(reason.decision == "block" ? Theme.dangerText : Theme.textMuted)
                                .fixedSize()
                        }
                    }
                }
            }
            recentRuns
        }
    }

    @ViewBuilder private var recentRuns: some View {
        if wantsRecent {
            let runs = stats.recentRuns(source: source, window: window)
            if let runs, !runs.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    sectionLabel("Recent runs")
                    ForEach(runs.prefix(Self.recentLimit)) { run in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(run.summary)
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                .lineLimit(1).truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Text(HookStatsFormat.relative(run.ts))
                                .font(Theme.caption).foregroundStyle(Theme.textMuted)
                                .fixedSize()
                        }
                    }
                }
            } else if runs == nil && stats.recentIsLoading(source: source, window: window) {
                VStack(alignment: .leading, spacing: 6) {
                    sectionLabel("Recent runs")
                    ForEach(0..<3, id: \.self) { _ in SkeletonBlock(width: nil, height: 12, radius: 3) }
                }
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(Text("Reading recent runs"))
            }
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text).font(Theme.control).foregroundStyle(Theme.textMuted)
    }

    private func tile(_ label: String, _ value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(Theme.caption).foregroundStyle(Theme.textMuted)
                .lineLimit(1)
            Text(value)
                .font(Theme.font(size: 16, weight: .semibold))
                .foregroundStyle(color)
                .lineLimit(1)
        }
        .fixedSize()
        .padding(.horizontal, 10).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(TileProbe(label: label))
        .background(Theme.subtle, in: .rect(cornerRadius: Theme.smallRadius))
        .overlay(RoundedRectangle(cornerRadius: Theme.smallRadius).strokeBorder(Theme.border, lineWidth: 1))
    }
}


/// A label column and its value, one fact per line in the hook's rail.
private struct InfoLine<Content: View>: View {
    var label: String
    @ViewBuilder var content: Content

    init(_ label: String, @ViewBuilder content: () -> Content) {
        self.label = label
        self.content = content()
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label).font(Theme.caption).foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .frame(width: Theme.scaled(76), alignment: .leading)
            content.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// MARK: - New hook sheet

/// `swe hooks new` writes the executable skeleton under
/// `~/.swe/hooks/<harness>`; the sheet then shows the stanza the reply
/// carries for the user to paste into a settings file (the app never
/// writes one), plus where that file is.
private struct NewHookSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismissSheet) private var dismiss
    /// The harnesses to pick from: the ones already holding hooks, plus
    /// every active harness swe knows.
    @State private var harnesses: [String] = []
    @State private var harness = "claude"
    @State private var name = ""
    @State private var error: String?
    @State private var working = false
    @State private var created: NewHookReply?

    /// `swe hooks new`'s own rule, from create.py: letters, digits, `_` and
    /// `-`, letter or digit first. Looser than a skill name on purpose.
    static let nameRule = /^[A-Za-z0-9][A-Za-z0-9_-]*$/
    var nameProblem: String? {
        if name.isEmpty { return "Letters, digits, '_' and '-', like prose-guard" }
        if name.count > 64 { return "At most 64 characters" }
        return name.wholeMatch(of: Self.nameRule) == nil
            ? "Letters, digits, '_' and '-', starting with a letter or digit" : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("New hook").font(Theme.sectionTitle).tracking(-0.5)
            FormField("Harness") {
                SelectMenu(options: harnesses.map { ($0, HarnessMark($0).name, $0) },
                           selection: $harness, width: nil)
            }
            FormField("Name") {
                TextField("prose-guard", text: $name)
                    .sweField(monospaced: true)
                if let nameProblem {
                    Text(nameProblem).font(Theme.caption).foregroundStyle(Theme.textMuted)
                }
            }
            if let created {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Created \(displayPath(created.path))", systemImage: "checkmark.circle")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Declare it yourself: swe never writes a settings file. Paste this into \(settingsList(created)):")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(alignment: .top, spacing: 6) {
                        Text(created.stanza)
                            .font(Theme.font(size: 12, design: .monospaced))
                            .lineSpacing(2)
                            .foregroundStyle(Theme.textSecondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 6)
                            .padding(.horizontal, 8)
                            .background(Theme.code, in: .rect(cornerRadius: Theme.smallRadius))
                            .overlay(RoundedRectangle(cornerRadius: Theme.smallRadius)
                                .strokeBorder(Theme.border, lineWidth: 1))
                        CopyButton { copyToPasteboard(created.stanza) }
                            .foregroundStyle(Theme.textSecondary)
                            .frame(width: 24, height: 28)
                            .contentShape(.rect)
                            .plainButton(cornerRadius: Theme.smallRadius)
                    }
                    Text("Then swe init links it into place.")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                }
            }
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
            }
            HStack {
                Spacer()
                if let created, let editor = app.editor {
                    Button("Open in \(editor.name)") {
                        NSWorkspace.shared.open([URL(fileURLWithPath: created.path)],
                                                withApplicationAt: editor.appURL,
                                                configuration: .init())
                    }
                    .secondaryButton()
                }
                Button(created == nil ? "Cancel" : "Done") { dismiss() }
                    .secondaryButton()
                    .disabled(working)
                if created == nil {
                    Button("Create") {
                        working = true
                        error = nil
                        Task {
                            do {
                                created = try await app.hooks.create(
                                    harness: harness, name: name,
                                    swe: app.swe, for: app.swePath)
                            } catch { self.error = error.localizedDescription }
                            working = false
                        }
                    }
                    .primaryButton()
                    .disabled(working || nameProblem != nil || harnesses.isEmpty)
                }
            }
        }
        .sheetScaffold(width: 460)
        .task {
            var names = Set(app.hooks.hooks.map(\.harness))
            if let listed = try? await app.swe.harnesses(withUsage: false) {
                names.formUnion(listed.filter { $0.state != .archived }.map(\.name))
            }
            harnesses = names.sorted()
            if !harnesses.contains(harness) { harness = harnesses.first ?? "claude" }
        }
    }

    /// "settings.json or hooks.json", a compact take on the reply's paths.
    private func settingsList(_ reply: NewHookReply) -> String {
        let names = reply.settings.map { displayPath($0) }
        return names.isEmpty ? "the harness's settings file" : names.joined(separator: " or ")
    }
}
