import AppKit
import SWECore
import SwiftUI

/// Benchmarks from Artificial Analysis crossed with the models the ledger
/// actually saw. Bars are provider-coloured, used models carry a light
/// outline, and every label hovers into the shared model card. The data
/// lands in `~/.swe/data/artificial-analysis/`; without a key the page
/// points at Settings › Services.
struct ModelsPage: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        if let model = app.models {
            ModelsContent(model: model)
        } else {
            Page(title: "Models",
                 subtitle: "Benchmarks for the models you use and the leaders.") {
                ErrorCard(message: app.overviewError ?? "The usage ledger is unavailable.")
            }
        }
    }
}

struct ModelsContent: View {
    @Bindable var model: ModelsModel
    @Environment(AppModel.self) private var app
    /// The logged name the link sheet is editing.
    @State private var linking: String?
    @State private var linkError: String?
    /// The toolbar row's width and its two fixed clusters' ideals — the
    /// "Updated …" stamp shows only when all three fit on one row.
    @State private var toolbarWidth: CGFloat = 0
    @State private var controlsWidth: CGFloat = 0
    @State private var trailingWidth: CGFloat = 0
    @AppStorage("settingsSection") private var settingsSection = ""

    static let attribution = URL(string: "https://artificialanalysis.ai")!

    var body: some View {
        Page(title: "Models",
             subtitle: "Benchmarks for the models you use and the leaders. Cost is LiteLLM's list price; benchmarks from",
             subtitleLink: ("Artificial Analysis.", Self.attribution)) {
            toolbar
            content
        }
        .task { await model.load() }
        .task(id: app.overview?.completedScans) {
            await model.loadIfNeeded(usageStamp: app.overview?.completedScans)
        }
        .cardSheet(isPresented: Binding(get: { linking != nil },
                                    set: { if !$0 { linking = nil; linkError = nil } })) {
            if let linking {
                LinkSheet(model: model, logged: linking, error: $linkError) {
                    do {
                        try model.link(logged: linking, to: $0)
                        self.linking = nil
                    } catch {
                        linkError = "Couldn't save the link: \(error.localizedDescription)"
                    }
                }
            }
        }
    }

    // MARK: - Toolbar

    /// The controls row: view mode, metric, scope, harness and period on
    /// the left; the freshness stamp, warning and reload buttons on the
    /// right. When the row runs out of room the stamp hides and its text
    /// moves into the reload button's hover tip. When even the controls,
    /// the search field's floor and the buttons don't fit (a narrow
    /// window), the controls wrap and the search field takes a row of its
    /// own, so the page never grows wider than the window.
    private var toolbar: some View {
        VStack(alignment: .leading, spacing: 0) {
            Color.clear.frame(height: 0)
                .background(alignment: .leading) {
                    // The controls' one-row width, from a hidden copy, so
                    // wrapping them never changes the figure it was based on.
                    HStack(spacing: 8) { controlItems }
                        .fixedSize().hidden().accessibilityHidden(true)
                        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { controlsWidth = $0 }
                }
            if fitsOneRow {
                HStack(spacing: 8) {
                    HStack(spacing: 8) { controlItems }.fixedSize()
                    search
                    Spacer(minLength: 8)
                    stamp
                    trailing
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .top, spacing: 8) {
                        FlowLayout(spacing: 8, lineSpacing: 8) { controlItems }
                        trailing
                    }
                    HStack(spacing: 8) {
                        search
                        stamp
                    }
                }
            }
        }
        // Exactly the width offered, whatever the row asks for: a row
        // wider than the page used to widen the page (and every card under
        // it) past the window, and measuring it then read that overflow
        // back as room, so the stamp kept itself on screen.
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { toolbarWidth = $0 }
    }

    @ViewBuilder private var controlItems: some View {
        Tabs(options: [(ModelsModel.ViewMode.chart, "Chart"),
                       (ModelsModel.ViewMode.table, "Table")],
             selection: Binding(get: { model.viewMode },
                                set: { model.set(viewMode: $0) }),
             style: .segmented)
        SelectMenu(options: ModelsModel.Metric.allCases.map { ($0, $0.title) },
                   selection: Binding(get: { model.metric },
                                      set: { model.set(metric: $0) }),
                   width: nil,
                   symbols: Dictionary(uniqueKeysWithValues:
                       ModelsModel.Metric.allCases.map { ($0, $0.symbol) }))
        let scopeMenu = SelectMenu(options: ModelsModel.Scope.allCases.map { ($0, $0.title) },
                                   selection: Binding(get: { model.scope },
                                                      set: { model.set(scope: $0) }),
                                   width: nil)
        // Off while a search runs, and only then does it need a tip.
        if model.searching {
            scopeMenu.disabled(true)
                .hoverTip("Searching the whole catalog. Clear the search to pick a scope.",
                          replacesLabel: false)
        } else {
            scopeMenu
        }
        MultiSelectMenu(allTitle: "All harnesses",
                        options: model.harnessOptions.map {
                            (value: $0, title: HarnessMark($0).name, icon: $0)
                        },
                        selection: Binding(get: { model.harnessFilter },
                                           set: { model.set(harnessFilter: $0) }),
                        countTitle: { "\($0) harnesses" })
        SelectMenu(options: OverviewPeriod.allCases.map { ($0, $0.title) },
                   selection: Binding(get: { model.period },
                                      set: { value in
                                          Task<Void, Never> { await model.set(period: value) }
                                      }),
                   width: nil)
    }

    private var search: some View {
        SearchField(prompt: Self.searchPrompt, text: $model.query)
            .frame(minWidth: Self.searchMinWidth)
    }

    @ViewBuilder private var stamp: some View {
        if let updatedAt = model.updatedAt, roomForStamp {
            Text("Updated \(updatedAt.formatted(.relative(presentation: .named)))")
                .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                .lineLimit(1).fixedSize()
        }
    }

    private var trailing: some View {
        HStack(spacing: 8) {
            if !model.unmatched.isEmpty {
                UnmatchedBadge(model: model) { linking = $0 }
            }
            reload
        }
        .fixedSize()
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { trailingWidth = $0 }
    }

    /// Short enough to read whole at the field's floor.
    static let searchPrompt = "Search all models"

    /// The search field's floor, wide enough for its whole prompt (a fixed
    /// 140 cut "Search the whole catalog" to "Search the whole cata"):
    /// the prompt at the field's font, plus the magnifier, the gap and the
    /// side padding. Below it the stamp gives up its room.
    /// Measured at the current text zoom, like the stamp.
    @MainActor static var searchMinWidth: CGFloat {
        let prompt = (searchPrompt as NSString).size(withAttributes: [.font: Theme.nsFont(size: 14)]).width
        return ceil(prompt) + Theme.scaled(14) + 8 + 2 * 12
    }

    private var stampWidth: CGFloat {
        guard let updatedAt = model.updatedAt else { return 0 }
        let text = "Updated \(updatedAt.formatted(.relative(presentation: .named)))"
        return (text as NSString).size(withAttributes: [.font: Theme.nsFont(size: 12)]).width
    }

    /// Whether the controls, the search field's floor and the buttons fit
    /// on one row. Until the widths are measured, assume they do.
    private var fitsOneRow: Bool {
        guard toolbarWidth > 0, controlsWidth > 0 else { return true }
        return Self.oneRowWidth(controls: controlsWidth, stamp: nil, trailing: trailingWidth) <= toolbarWidth
    }

    /// Whether the stamp fits: beside everything on one row, or beside
    /// the search field's floor on the wrapped layout's second row.
    private var roomForStamp: Bool {
        guard model.updatedAt != nil, toolbarWidth > 0 else { return false }
        if fitsOneRow {
            return Self.oneRowWidth(controls: controlsWidth, stamp: stampWidth,
                                    trailing: trailingWidth) <= toolbarWidth
        }
        return Self.searchMinWidth + Self.gap + stampWidth <= toolbarWidth
    }

    static let gap: CGFloat = 8

    /// The one-row toolbar's narrowest width: controls, search floor,
    /// spacer floor, the stamp if any and the buttons, with the HStack's
    /// gap between every pair (the spacer is an item too).
    @MainActor static func oneRowWidth(controls: CGFloat, stamp: CGFloat?, trailing: CGFloat) -> CGFloat {
        let items = [controls, searchMinWidth, gap /* Spacer(minLength:) */, stamp, trailing].compactMap { $0 }
        return items.reduce(0, +) + gap * CGFloat(items.count - 1)
    }

    /// The freshness stamp lives here too, for when the row drops it.
    private var reloadHelp: String {
        let base = model.refreshError
            ?? "Fetch the latest benchmarks from Artificial Analysis"
        guard let updatedAt = model.updatedAt else { return base }
        return "Updated \(updatedAt.formatted(.relative(presentation: .named))). \(base)"
    }

    private var reload: some View {
        Button { Task { await model.refresh() } } label: {
            Image(systemName: "arrow.clockwise")
                .rotationEffect(.degrees(model.isRefreshing ? 360 : 0))
                .animation(model.isRefreshing
                           ? .linear(duration: 1).repeatForever(autoreverses: false)
                           : .default,
                           value: model.isRefreshing)
        }
        .iconButton()
        .disabled(model.isRefreshing || !model.hasKey)
        // A stable name; the stamp or the last error is the hint.
        .accessibilityLabel("Reload benchmarks")
        .hoverTip(reloadHelp, replacesLabel: false)
    }

    // MARK: - Content

    @ViewBuilder private var content: some View {
        // A search with hits shows them even when the AA feed is empty:
        // LiteLLM-only keys need no benchmarks.
        if model.searching && !model.rows.isEmpty {
            results
        } else if !model.hasLoaded && model.allModels.isEmpty {
            // Before the first key check `hasKey` is false: wait for it
            // rather than flash the no-key card on every launch.
            ModelsSkeleton(model: model, label: "Loading benchmarks…")
        } else if !model.hasKey && !model.isLoading && model.allModels.isEmpty {
            // Cached benchmarks still show after the key is removed.
            noKey
        } else if model.allModels.isEmpty {
            if model.isLoading || model.isRefreshing {
                ModelsSkeleton(model: model, label: "Fetching benchmarks…")
            } else if let error = model.refreshError {
                ErrorCard(message: "Couldn't fetch benchmarks: \(error)")
            } else if model.searching {
                EmptyCard(text: "No model or LiteLLM price key matches.",
                          icon: "magnifyingglass")
            } else {
                EmptyCard(text: "No benchmark data yet.", icon: "cube.transparent")
            }
        } else if model.rows.isEmpty {
            if model.searching {
                EmptyCard(text: "No model or LiteLLM price key matches.",
                          icon: "magnifyingglass")
            } else if model.usagePending {
                ModelsSkeleton(model: model, rows: 4, label: "Reading your usage…")
            } else {
                EmptyCard(text: emptyText, icon: "chart.bar")
            }
        } else {
            results
        }
    }

    /// The chart or table, plus the overflow note when a search had to
    /// cap the LiteLLM-only tail.
    private var results: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch model.viewMode {
            case .chart: ChartCard(model: model)
            case .table: TableCard(model: model)
            }
            if model.searchOverflow > 0 {
                Text("+\(model.searchOverflow) more matches. Tighten the query to list them.")
                    .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 4)
            }
        }
    }

    /// Why there are no rows: usage the matcher couldn't place is still
    /// usage, and a filtered Top 25 can simply miss every model.
    private var emptyText: String {
        // First: linking can't help a filtered board the models miss.
        if !model.harnessFilter.isEmpty && model.scope == .top25 {
            return "None of the top 25 ran in these harnesses this period."
        }
        // A name the matcher missed may still be on AA, under another name.
        if !model.unmatched.isEmpty {
            return model.unmatched.count == 1
                ? "SWE couldn't match the model you used to Artificial Analysis. Link it from the ⚠︎ button."
                : "SWE couldn't match the models you used to Artificial Analysis. Link them from the ⚠︎ button."
        }
        return "No usage in this period."
    }

    /// No key: point at Settings › Integrations instead of an empty chart.
    private var noKey: some View {
        Card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Add an Artificial Analysis key in Settings › Integrations")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                Text("The Models page needs a free Artificial Analysis API key to download benchmarks. 100 requests a day are free.")
                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                HStack(spacing: 12) {
                    Button("Open Settings › Integrations") {
                        settingsSection = SettingsSection.integrations.rawValue
                        app.selection = .settings
                    }
                    .secondaryButton()
                    if let aa = ServiceCredentials.services.first(where: { $0.id == "artificial-analysis" }) {
                        Link(destination: aa.keyURL) {
                            Text("Get a key").font(Theme.caption).foregroundStyle(Theme.link)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Warning popover

/// The ⚠︎ button: used models the matcher couldn't place, each opening
/// the link sheet on click.
private struct UnmatchedBadge: View {
    var model: ModelsModel
    var open: (String) -> Void

    @State private var show = false
    @State private var rest: Task<Void, Never>?

    var body: some View {
        Button { show.toggle() } label: {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(Theme.warningText)
        }
        .iconButton()
        .help(model.unmatched.count == 1
              ? "1 model you used isn't on Artificial Analysis"
              : "\(model.unmatched.count) models you used aren't on Artificial Analysis")
        .onHover { inside in
            if inside {
                rest = Task {
                    try? await Task.sleep(for: .milliseconds(150))
                    guard !Task.isCancelled else { return }
                    show = true
                }
            } else {
                rest?.cancel()
            }
        }
        .popover(isPresented: $show, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(model.unmatched) { row in
                    Button { show = false; open(row.model) } label: {
                        HStack(spacing: 6) {
                            BrandLogo(model: row.model, size: 14)
                            Text(row.model).font(Theme.caption).foregroundStyle(Theme.textPrimary)
                                .lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Text(UsageFormat.tokens(row.tokens))
                                .font(Theme.caption).foregroundStyle(Theme.textSecondary).monospacedDigit()
                        }
                        .padding(.horizontal, 10).padding(.vertical, 7)
                        .frame(width: 300)
                        .contentShape(.rect)
                    }
                    .plainButton(cornerRadius: 0)
                    .modifier(RowHover())
                }
            }
            .padding(.vertical, 6)
        }
    }
}

// MARK: - Chart

/// The metric chart: one provider-coloured bar per model, best first, the
/// value inside the bar and the logo plus angled name under it. Used
/// models keep a light inset outline; bars scroll before they shrink past
/// 28pt. Hovering a bar (the bar itself, not its column or label) shows
/// its model card beside it: one card for the whole chart, moved and
/// refilled as the pointer crosses bars, so neighbouring bars never open
/// cards of their own.
struct ChartCard: View {
    @Bindable var model: ModelsModel

    /// Bars never get narrower than this; the row scrolls instead.
    static let minColumnWidth: CGFloat = 36
    /// The bar zone; every bar bottoms out on the same baseline.
    static let barHeight: CGFloat = 280
    /// The provider logo and the zone it sits in.
    static let logoSize: CGFloat = 18
    static let logoHeight: CGFloat = 28
    /// The label zone: each name ends under its bar's middle and runs
    /// down-left at `labelAngle`, Artificial Analysis style.
    static let labelHeight: CGFloat = 150
    static let labelAngle: Double = 60
    static let labelFont: CGFloat = 10.5
    /// A name's cap along its slant: as long as the zone is deep.
    static let labelRun: CGFloat = 160
    /// The strip's side padding and the gap between bars.
    static let inset: CGFloat = 6
    static let spacing: CGFloat = 8
    /// The space the hover card is laid out in.
    private nonisolated static let space = "modelChart"

    @State private var width: CGFloat = 0
    /// Which bar's card shows, and the waits around it.
    @State private var hover = ChartHover()
    static let openDelay = ChartHover.openDelay
    /// The bar strip's frame in the chart's space; moves as it scrolls.
    @State private var strip: CGRect = .zero
    /// The chart area's size, for keeping the card inside it.
    @State private var area: CGSize = .zero
    @State private var cardHeight: CGFloat = 0

    private var columnWidth: CGFloat {
        guard !model.rows.isEmpty else { return Self.minColumnWidth }
        return max(Self.minColumnWidth, (width - 2 * Self.inset - leadingRoom) / CGFloat(model.rows.count))
    }

    /// Extra space before the first bar so its slanted name, which runs
    /// left of the bar's middle, isn't cut off by the strip's edge.
    private var leadingRoom: CGFloat {
        Self.leadingRoom(reach: model.rows.first.map(BarColumn.labelReach) ?? 0,
                         width: width, count: model.rows.count)
    }

    /// The padding P that puts the first bar's middle at least `reach`
    /// from the strip's edge, against the column width that results once
    /// P itself is taken out: P + inset + (column − spacing) / 2 ≥ reach,
    /// with column = (width − 2·inset − P) / count, or the minimum width
    /// once that floors.
    static func leadingRoom(reach: CGFloat, width: CGFloat, count: Int) -> CGFloat {
        guard count > 0, reach > 0 else { return 0 }
        let n = CGFloat(count)
        let flexible = (reach - inset + spacing / 2 - (width - 2 * inset) / (2 * n)) / (1 - 1 / (2 * n))
        var room = max(0, flexible)
        if (width - 2 * inset - room) / n < minColumnWidth {
            room = max(0, reach - inset - (minColumnWidth - spacing) / 2)
        }
        return room.rounded(.up)
    }

    /// The tallest bar: every height is a fraction of it. Older scores
    /// share the scale, so their dashed bars keep their own proportions.
    private var peak: Double {
        model.rows.compactMap { $0.value ?? model.metric.pastScore(of: $0.aa)?.value }.max() ?? 1
    }

    /// The first row drawn from an older index version, where the divider
    /// goes; nil when the chart has none.
    private var firstPastRow: String? {
        model.rows.first { $0.value == nil && model.metric.pastScore(of: $0.aa) != nil }?.id
    }

    var body: some View {
        Card(title: model.metric.chartTitle, subtitle: model.metric.detail) {
            VStack(alignment: .leading, spacing: 10) {
                Color.clear.frame(maxWidth: .infinity, maxHeight: 0)
                    .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: Self.spacing) {
                        ForEach(model.rows) { row in
                            BarColumn(row: row, metric: model.metric, peak: peak,
                                      startsPastGroup: row.id == firstPastRow) { inside in
                                if inside { hover.enter(row.id) } else { hover.leave(row.id) }
                            }
                            .frame(width: columnWidth - Self.spacing)
                        }
                    }
                    .padding(.horizontal, Self.inset)
                    .padding(.leading, leadingRoom)
                    .onGeometryChange(for: CGRect.self) {
                        $0.frame(in: .named(Self.space))
                    } action: { strip = $0 }
                }
                .scrollIndicators(.hidden)
                // Page enables selectable text. Rotated chart labels must stay SwiftUI
                // text: AppKit's selectable-text layout aborts in _nsis_frameInEngine.
                .textSelection(.disabled)
                legend
                HStack {
                    Text("Hover a bar for the model card")
                        .font(Theme.caption).foregroundStyle(Theme.textMuted)
                    Spacer()
                    Link("Data from Artificial Analysis", destination: ModelsContent.attribution)
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            .coordinateSpace(.named(Self.space))
            .onGeometryChange(for: CGSize.self) { $0.size } action: { area = $0 }
            .overlay(alignment: .topLeading) { hoverCard }
        }
    }

    /// The card beside the hovered bar: to its right, or to its left
    /// when the right would run out of the chart, top-aligned with the
    /// tallest bar and kept inside the chart area.
    @ViewBuilder private var hoverCard: some View {
        if let index = hover.index(in: model.rows.map(\.id)) {
            let left = strip.minX + Self.inset + leadingRoom + CGFloat(index) * columnWidth
            let right = left + columnWidth - Self.spacing
            let gap: CGFloat = 10
            let x = right + gap + ModelCard.width <= area.width
                ? right + gap
                : max(0, left - gap - ModelCard.width)
            let y = min(max(strip.minY, 0), max(0, area.height - cardHeight))
            ModelCard(row: model.rows[index], period: model.period)
                .background(Theme.card, in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.border, lineWidth: 1))
                .shadow(color: .black.opacity(0.25), radius: 16, y: 6)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { cardHeight = $0 }
                .textSelection(.disabled)
                .allowsHitTesting(false)
                .offset(x: x, y: y)
        }
    }

    /// One swatch per provider, ordered by first appearance in the chart.
    private var legend: some View {
        var order: [ModelVendor] = []
        var seen: Set<String> = []
        for row in model.rows {
            guard let aa = row.aa else { continue }
            let vendor = ModelVendor.of(aa)
            if seen.insert(vendor.id).inserted { order.append(vendor) }
        }
        return FlowLayout(spacing: 14, lineSpacing: 6) {
            ForEach(order, id: \.id) { vendor in
                HStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color(rgb: vendor.color))
                        .frame(width: 10, height: 10)
                    BrandLogo(vendor: vendor, size: 12)
                    Text(vendor.name)
                        .font(Theme.font(size: 10.5)).foregroundStyle(Theme.textSecondary)
                }
            }
            if firstPastRow != nil {
                HStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 2)
                        .strokeBorder(Theme.textSecondary, style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
                        .frame(width: 10, height: 10)
                    Text(Self.pastLegend)
                        .font(Theme.font(size: 10.5)).foregroundStyle(Theme.textSecondary)
                }
            }
        }
    }

    /// The legend entry for dashed bars.
    static let pastLegend = "Older index version, not on the same scale"
}

private struct BarColumn: View {
    var row: ModelsModel.Row
    var metric: ModelsModel.Metric
    /// The chart's largest value; heights are fractions of it.
    var peak: Double
    /// The first bar of the older-index group: a divider sits before it.
    var startsPastGroup = false
    /// The pointer entered (true) or left (false) the bar itself.
    var hover: (Bool) -> Void

    private var vendor: ModelVendor { row.vendor }

    /// The older score this bar draws, dashed, when the model has no
    /// current one.
    private var past: AAPastScore? {
        row.value == nil ? metric.pastScore(of: row.aa) : nil
    }

    /// A dashed bar holds two lines, the number and its tag.
    static let pastMinHeight: CGFloat = 34

    /// Bar text reads dark on light fills (OpenAI's grey) — a fixed
    /// near-black, not the theme's dark-mode white.
    private var valueColor: Color {
        vendor.color.luminance > 0.45 ? Color(hex: "#0a0a0a") : .white
    }

    /// The name cut at `labelRun` (head ellipsis), so the end under the
    /// bar always shows. Measured at the label font.
    static func label(_ row: ModelsModel.Row) -> String {
        let name = row.displayName
        var cut = Substring(name)
        while !cut.isEmpty, width(of: "…" + cut) > ChartCard.labelRun {
            cut = cut.dropFirst()
        }
        return cut.count == name.count ? name : "…" + cut
    }

    /// How far left of the bar's middle the slanted name reaches: its
    /// length along the slant plus the half line height its far corner
    /// sticks out sideways.
    static func labelReach(_ row: ModelsModel.Row) -> CGFloat {
        let angle = ChartCard.labelAngle * .pi / 180
        let lineHeight = Theme.nsFont(size: ChartCard.labelFont).boundingRectForFont.height
        return width(of: label(row)) * cos(angle) + lineHeight / 2 * sin(angle)
    }

    private static func width(of text: String) -> CGFloat {
        let attrs: [NSAttributedString.Key: Any] = [.font: Theme.nsFont(size: ChartCard.labelFont)]
        return (text as NSString).size(withAttributes: attrs).width
    }

    var body: some View {
        VStack(spacing: 0) {
            // Zone 1: the bar, bottom-aligned on the shared baseline.
            let past = past
            let shown = row.value ?? past?.value
            let fraction = shown.map { max($0 / max(peak, .leastNonzeroMagnitude), 0) } ?? 0
            let fill: AnyShapeStyle = past != nil ? AnyShapeStyle(Color(rgb: vendor.color).opacity(0.12))
                : row.value == nil ? AnyShapeStyle(Theme.subtle) : AnyShapeStyle(Color(rgb: vendor.color))
            RoundedRectangle(cornerRadius: 4)
                .fill(fill)
                .frame(height: shown == nil ? 8
                       : max(fraction * ChartCard.barHeight, past == nil ? 20 : Self.pastMinHeight))
                .overlay {
                    // An older-index score: the lab's colour as a dashed
                    // outline, so it never reads as a current bar.
                    if past != nil {
                        RoundedRectangle(cornerRadius: 4)
                            .strokeBorder(Color(rgb: vendor.color),
                                          style: StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                    }
                }
                .overlay(alignment: .top) {
                    if let past {
                        VStack(spacing: 1) {
                            Text(metric.format(past.value))
                                .font(Theme.font(size: 10, weight: .medium)).monospacedDigit()
                                .foregroundStyle(Theme.textPrimary)
                            Text(past.tag)
                                .font(Theme.font(size: 8.5)).foregroundStyle(Theme.textSecondary)
                        }
                        .lineLimit(1).minimumScaleFactor(0.6)
                        .padding(.top, 4).padding(.horizontal, 2)
                        .accessibilityElement(children: .combine)
                        .hoverTip(past.note, replacesLabel: false)
                    } else if let value = row.value {
                        Text(metric.format(value))
                            .font(Theme.font(size: 10, weight: .medium)).monospacedDigit()
                            .foregroundStyle(valueColor)
                            .lineLimit(1).minimumScaleFactor(0.6)
                            .padding(.top, 4)
                    }
                }
                .overlay {
                    if row.used, past == nil {
                        RoundedRectangle(cornerRadius: 4)
                            .strokeBorder(.white.opacity(0.9), lineWidth: 1.5)
                            .padding(1)
                    }
                }
                // Only the bar opens the card, not the empty space above
                // it or the logo and name below.
                .contentShape(.rect)
                .onHover(perform: hover)
                .frame(height: ChartCard.barHeight, alignment: .bottom)
                // A model with no figure keeps its 8pt stub, labelled so it
                // doesn't read as a bar that failed to draw.
                .overlay(alignment: .bottomLeading) {
                    // The divider before the first older-index bar, in the
                    // gap between it and the last current one.
                    if startsPastGroup {
                        Rectangle()
                            .fill(Theme.border)
                            .frame(width: 1, height: ChartCard.barHeight)
                            .offset(x: -(ChartCard.spacing + 1) / 2)
                            .allowsHitTesting(false)
                    }
                }
                .overlay(alignment: .bottom) {
                    if shown == nil {
                        Text("No score")
                            .font(Theme.font(size: 10)).foregroundStyle(Theme.textMuted)
                            .lineLimit(1).minimumScaleFactor(0.6)
                            .padding(.bottom, 12)
                            .hoverTip(metric.missingNote ?? "Artificial Analysis has no \(metric.title.lowercased()) figure for this model.",
                                      replacesLabel: false)
                    }
                }
            // Zone 2: the provider logo.
            BrandLogo(vendor: vendor, size: ChartCard.logoSize)
                .frame(height: ChartCard.logoHeight)
            // Zone 3: the name. Its end sits on a point under the bar's
            // middle and it runs down-left at `labelAngle`, so every name
            // lands on its own bar whatever its length.
            Color.clear
                .frame(height: ChartCard.labelHeight)
                .overlay(alignment: .top) {
                    Color.clear.frame(width: 0, height: 0)
                        .overlay(alignment: .trailing) {
                            Text(Self.label(row))
                                .font(Theme.font(size: ChartCard.labelFont))
                                .foregroundStyle(row.used ? Theme.textPrimary : Theme.textSecondary)
                                .lineLimit(1)
                                .fixedSize()
                                .rotationEffect(.degrees(-ChartCard.labelAngle), anchor: .trailing)
                        }
                        .padding(.top, 6)
                }
        }
    }
}

// MARK: - Table

/// The same rows as a table: who ran it, the headline benchmarks, the
/// LiteLLM list price the ledger charges, AA's blended price and cost
/// rank beside it, and the user's tokens. The benchmark columns head with
/// an icon; it and the two price headers open a tip naming the measure.
struct TableCard: View {
    @Bindable var model: ModelsModel
    @State private var width: CGFloat = 0

    var body: some View {
        let columns = TableColumns.widths(for: width, extraMetric: model.showsExtraMetric,
                                          hidden: model.emptyBenchmarks)
        Card {
            // Narrow windows scroll sideways instead of crushing the
            // columns below their minimum widths.
            ScrollView(.horizontal) {
                VStack(spacing: 0) {
                    header(columns)
                    Divider().overlay(Theme.border)
                    // Rows build lazily against the page's vertical
                    // scroll: a broad search can list a hundred of them,
                    // and each carries icons, hover tips and a gradient.
                    LazyVStack(spacing: 0) {
                        ForEach(model.rows) { row in
                            TableRow(row: row, model: model, columns: columns)
                            Divider().overlay(Theme.rowDivider)
                        }
                    }
                }
                .frame(width: columns.total, alignment: .leading)
            }
            .scrollIndicators(.automatic)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        }
    }

    private func header(_ columns: TableColumns) -> some View {
        HStack(spacing: TableColumns.gap) {
            Text("Model").frame(width: columns.model, alignment: .leading)
            Text("Ran in").frame(width: columns.ranIn, alignment: .leading)
            benchmarkHeader(.intelligence).frame(width: columns.intelligence, alignment: .trailing)
            if columns.coding > 0 {
                benchmarkHeader(.coding).frame(width: columns.coding, alignment: .trailing)
            }
            if columns.reasoning > 0 {
                benchmarkHeader(.reasoning).frame(width: columns.reasoning, alignment: .trailing)
            }
            benchmarkHeader(.speed).frame(width: columns.speed, alignment: .trailing)
            // The picked metric shows when it has no column of its own.
            if model.showsExtraMetric {
                benchmarkHeader(model.metric).frame(width: columns.extra, alignment: .trailing)
            }
            Text("Cost / 1M")
                .hoverTip(ColumnTip.listPrice.text, title: ColumnTip.listPrice.title)
                .padding(.leading, TableColumns.priceInset)
                .frame(width: columns.price, alignment: .leading)
            Text("AA blended")
                .hoverTip(ColumnTip.blended.text, title: ColumnTip.blended.title)
                .frame(width: columns.cost, alignment: .leading)
            Text("Your tokens").frame(width: columns.tokens, alignment: .trailing)
        }
        .font(Theme.caption).foregroundStyle(Theme.tableHeaderText)
        .lineLimit(1)
        .padding(.vertical, 6)
    }
}

/// A benchmark column's header: the metric's short name in words (an
/// icon alone left the reader guessing), with a tip naming what it
/// measures.
@MainActor private func benchmarkHeader(_ metric: ModelsModel.Metric) -> some View {
    let tip = ColumnTip.of(metric)
    return Text(metric.columnTitle)
        .hoverTip(tip.text, title: tip.title)
}

/// What a header's tip says: "<Domain>: <measure>" in bold, then a line
/// on what the number means.
struct ColumnTip: Equatable {
    var title: String
    var text: String

    static func of(_ metric: ModelsModel.Metric) -> ColumnTip {
        switch metric {
        case .intelligence:
            ColumnTip(title: "Intelligence: Artificial Analysis Intelligence Index",
                      text: "A blend of Artificial Analysis's independent evaluations: reasoning, knowledge, coding and agentic tasks. Higher is better.")
        case .coding:
            ColumnTip(title: "Coding: Terminal-Bench 4.0",
                      text: "Percent of agentic coding and terminal tasks passed, run by Artificial Analysis. It replaces their Coding Index, which stopped covering new models in September 2026.")
        case .reasoning:
            ColumnTip(title: "Reasoning: Humanity's Last Exam",
                      text: "Percent of expert-level questions answered correctly, about 40% of them maths. It replaces Artificial Analysis's Maths Index, retired in January 2026.")
        case .speed:
            ColumnTip(title: "Speed: Mean output speed",
                      text: "Output tokens per second (t/s), the mean across the providers Artificial Analysis tests (older stored rows hold the median).")
        case .ttft:
            ColumnTip(title: "Latency: Time to first token",
                      text: "Mean seconds until the first token arrives, lower is better (older stored rows hold the median).")
        case .cost:
            blended
        }
    }

    static let listPrice = ColumnTip(
        title: "Cost: LiteLLM list price",
        text: "Per 1M tokens: input · output · cache read. The price the ledger charges your usage; hover a row's price for its key and cache-write rate.")

    static let blended = ColumnTip(
        title: "Cost: Artificial Analysis blended price",
        text: "One price per 1M tokens, weighting input to output 3 to 1. The bar ranks it against every priced model on Artificial Analysis, cheapest on the left.")
}

/// The table's column widths: each column has a floor that fits its
/// content and a weight for its share of any room above the floors, so
/// the model name no longer takes half the table while cost and the
/// figures are squeezed. Below the floors' sum the table scrolls.
struct TableColumns: Equatable {
    static let gap: CGFloat = 14
    /// Extra room before the left-aligned prices, so they don't run into
    /// the right-aligned speed figures beside them.
    static let priceInset: CGFloat = 12

    /// (floor, weight) per column, in display order.
    static let model = (min: 180.0, weight: 24.0)
    static let ranIn = (min: 56.0, weight: 6.0)
    static let intelligence = (min: 72.0, weight: 6.0)
    static let coding = (min: 48.0, weight: 6.0)
    static let reasoning = (min: 64.0, weight: 6.0)
    static let speed = (min: 44.0, weight: 6.0)
    static let extra = (min: 44.0, weight: 6.0)
    static let price = (min: 162.0, weight: 12.0)
    static let cost = (min: 130.0, weight: 16.0)
    static let tokens = (min: 76.0, weight: 9.0)

    var model: CGFloat, ranIn: CGFloat, intelligence: CGFloat, coding: CGFloat
    var reasoning: CGFloat, speed: CGFloat, extra: CGFloat, price: CGFloat, cost: CGFloat, tokens: CGFloat

    /// Every column plus the gaps between them.
    var total: CGFloat {
        let columns = [model, ranIn, intelligence, coding, reasoning, speed, extra, price, cost, tokens]
        let shown = columns.filter { $0 > 0 }
        return shown.reduce(0, +) + Self.gap * CGFloat(max(shown.count - 1, 0))
    }

    /// `hidden` drops benchmark columns no row has a figure for: a column
    /// of dashes says nothing the missing header doesn't.
    static func widths(for available: CGFloat, extraMetric: Bool,
                       hidden: Set<ModelsModel.Metric> = []) -> TableColumns {
        let none = (min: 0.0, weight: 0.0)
        let specs = [model, ranIn, intelligence,
                     hidden.contains(.coding) ? none : coding,
                     hidden.contains(.reasoning) ? none : reasoning, speed,
                     extraMetric ? extra : none, price, cost, tokens]
        let count = specs.filter { $0.min > 0 }.count
        let floors = specs.map(\.min).reduce(0, +) + gap * CGFloat(count - 1)
        let room = max(0, available - floors)
        let weights = specs.map(\.weight).reduce(0, +)
        let w = specs.map { CGFloat($0.min) + (weights > 0 ? room * CGFloat($0.weight) / CGFloat(weights) : 0) }
        return TableColumns(model: w[0], ranIn: w[1], intelligence: w[2], coding: w[3],
                            reasoning: w[4], speed: w[5], extra: w[6], price: w[7], cost: w[8], tokens: w[9])
    }
}

extension ModelsModel {
    /// The picked metric gets a table column when no fixed one shows it.
    var showsExtraMetric: Bool { metric == .ttft }

    /// The optional benchmark columns no listed row has a score for.
    var emptyBenchmarks: Set<ModelsModel.Metric> {
        var empty: Set<ModelsModel.Metric> = []
        if !rows.isEmpty, rows.allSatisfy({ $0.aa?.coding == nil }) { empty.insert(.coding) }
        if !rows.isEmpty, rows.allSatisfy({ $0.aa?.reasoning == nil }) { empty.insert(.reasoning) }
        return empty
    }
}

extension ModelsModel.Metric {
    /// The table header's words.
    var columnTitle: String {
        switch self {
        case .intelligence: "Intelligence"
        case .coding: "Coding"
        case .reasoning: "Reasoning"
        case .speed: "Speed"
        case .ttft: "TTFT"
        case .cost: "Cost"
        }
    }
}

private struct TableRow: View {
    var row: ModelsModel.Row
    var model: ModelsModel
    var columns: TableColumns

    private var vendor: ModelVendor { row.vendor }

    var body: some View {
        HStack(spacing: TableColumns.gap) {
            // The lab's logo from ModelBrands, the same one the chart
            // draws under the bar; an empty slot keeps names aligned.
            HStack(spacing: 8) {
                BrandLogo(vendor: vendor, size: 16)
                ModelName(name: row.displayName, qualifier: row.qualifier,
                          rawID: row.alias == nil ? nil : row.logged)
            }
            .frame(width: columns.model, alignment: .leading)
            HStack(spacing: 4) {
                ForEach(row.harnesses.prefix(3), id: \.self) { harness in
                    HarnessIcon(harness: harness, size: 14)
                        .help(HarnessMark(harness).name)
                        .accessibilityLabel(HarnessMark(harness).name)
                }
                if row.harnesses.count > 3 {
                    Text("+\(row.harnesses.count - 3)")
                        .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                }
            }
            .frame(width: columns.ranIn, alignment: .leading)
            intelligence(row.aa).frame(width: columns.intelligence, alignment: .trailing)
            if columns.coding > 0 {
                metric(row.aa?.coding, missing: AALanguageModel.noCodingScore)
                    .frame(width: columns.coding, alignment: .trailing)
            }
            if columns.reasoning > 0 {
                metric(row.aa?.reasoning, missing: AALanguageModel.noReasoningScore)
                    .frame(width: columns.reasoning, alignment: .trailing)
            }
            metric(row.aa?.speed).frame(width: columns.speed, alignment: .trailing)
            if model.showsExtraMetric {
                // The unit lives in the header's tip, as for the others.
                let figure = Text(row.value.map { String(format: "%.2f", $0) } ?? "—")
                    .font(Theme.bodySmall).monospacedDigit()
                    .foregroundStyle(row.value == nil ? Theme.textMuted : Theme.textPrimary)
                    .frame(width: columns.extra, alignment: .trailing)
                if row.value == nil, let note = model.metric.missingNote {
                    figure.hoverTip(note, replacesLabel: false)
                } else {
                    figure
                }
            }
            ListPriceCell(price: row.listPrice)
                .padding(.leading, TableColumns.priceInset)
                .frame(width: columns.price, alignment: .leading)
            CostScale(model: row.aa, rank: row.costPercentile)
                .frame(width: columns.cost, alignment: .leading)
            Group {
                if model.usagePending {
                    // The usage read is still out: a spinner, not "—".
                    ProgressView().controlSize(.mini)
                } else {
                    Text(row.tokens > 0 ? UsageFormat.tokens(row.tokens) : "—")
                }
            }
            .font(Theme.bodySmall).monospacedDigit().foregroundStyle(Theme.textSecondary)
            .frame(width: columns.tokens, alignment: .trailing)
        }
        .padding(.vertical, 8)
    }

    /// The intelligence index, or the last one AA published in muted
    /// italics with its tag ("39 · Oct ’26") when the current feed has none.
    @ViewBuilder private func intelligence(_ aa: AALanguageModel?) -> some View {
        if let past = ModelsModel.Metric.intelligence.pastScore(of: aa) {
            Text("\(Int(past.value.rounded())) · \(past.tag)")
                .font(Theme.bodySmall).italic().monospacedDigit().foregroundStyle(Theme.textMuted)
                .lineLimit(1).minimumScaleFactor(0.7)
                .help(past.note)
                .hoverTip(past.note, replacesLabel: false)
        } else {
            metric(aa?.intelligence)
        }
    }

    /// A figure, or "—" when Artificial Analysis has none, with `missing`
    /// as the dash's hover note. Units are in the header's tip.
    @ViewBuilder private func metric(_ value: Double?, missing: String? = nil) -> some View {
        if let value {
            Text("\(Int(value.rounded()))")
                .font(Theme.bodySmall).monospacedDigit().foregroundStyle(Theme.textPrimary)
        } else {
            let dash = Text("—").font(Theme.bodySmall).foregroundStyle(Theme.textMuted)
            if let missing {
                dash.hoverTip(missing, replacesLabel: false)
            } else {
                dash
            }
        }
    }
}

/// The Cost column: the list price per 1M input, output and cache-read
/// tokens, with the key, cache-write rate and source on hover.
private struct ListPriceCell: View {
    var price: ModelsModel.ListPrice?

    var body: some View {
        if let price {
            Text([price.input, price.output, price.cacheRead].map(ModelCard.price).joined(separator: " · "))
                .font(Theme.bodySmall).monospacedDigit().foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
                .hoverTip(Self.help(price), replacesLabel: false)
        } else {
            Text("—").font(Theme.bodySmall).foregroundStyle(Theme.textMuted)
                .hoverTip("No LiteLLM price for this model", replacesLabel: false)
        }
    }

    static func help(_ price: ModelsModel.ListPrice) -> String {
        let source = switch price.source {
        case .liteLLM: "LiteLLM"
        case .swe: "SWE's price list"
        case .yours: "your price"
        }
        return "\(price.key), from \(source)\nCache write \(ModelCard.price(price.cacheWrite)) / 1M"
    }
}

/// The AA blended column: a green→purple gradient as wide as the column allows,
/// the model's rank as a white dot on it, and the blended price in a slot
/// of its own. An unknown price (decoded as nil) shows "—" and no dot.
private struct CostScale: View {
    var model: AALanguageModel?
    var rank: Double?

    private var price: Double? { model?.priceBlended }

    var body: some View {
        HStack(spacing: 10) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(LinearGradient(colors: [Color(hex: "#2fa35a"), Color(hex: "#e8913a"),
                                                      Color(hex: "#a46ae0")],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(height: 6)
                        .opacity(price == nil ? 0.35 : 1)
                    if let rank, price != nil {
                        Circle().fill(.white).frame(width: 8, height: 8)
                            .shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
                            .position(x: min(max(rank * geo.size.width, 4), geo.size.width - 4),
                                      y: geo.size.height / 2)
                    }
                }
            }
            .frame(height: 12)
            .frame(maxWidth: .infinity)
            Group {
                if let price {
                    Text(UsageFormat.dollars(price)).foregroundStyle(Theme.textSecondary)
                } else {
                    Text("—").foregroundStyle(Theme.textMuted)
                }
            }
            .font(Theme.caption).monospacedDigit()
            .frame(width: 52, alignment: .trailing)
        }
    }
}

// MARK: - Link sheet

/// "Link to a model": a searchable list of every AA model, picked to
/// teach the matcher what a logged name means. Writes model-links.json.
private struct LinkSheet: View {
    var model: ModelsModel
    var logged: String
    @Binding var error: String?
    var pick: (String) -> Void

    @State private var query = ""

    private var candidates: [AALanguageModel] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let all = model.allModels.sorted {
            ($0.intelligence ?? 0, $0.name) > ($1.intelligence ?? 0, $1.name)
        }
        guard !q.isEmpty else { return all }
        return all.filter {
            $0.name.lowercased().contains(q) || $0.slug.lowercased().contains(q)
                || $0.creatorName.lowercased().contains(q)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Link to a model").font(Theme.sectionTitle).tracking(-0.5)
                Text("“\(logged)” isn't on Artificial Analysis. Pick the model it should count as; the link is saved in model-links.json.")
                    .font(Theme.bodySmall).foregroundStyle(Theme.textSecondary)
            }
            if let error {
                Text(error).font(Theme.caption).foregroundStyle(Theme.dangerText)
            }
            TextField("Search models…", text: $query)
                .sweField()
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(candidates) { aa in
                        Button { pick(aa.id) } label: {
                            HStack(spacing: 8) {
                                let vendor = ModelVendor.of(aa)
                                // As every view naming a model: its lab's logo
                                // first, a blank square when there is none.
                                BrandLogo(vendor: vendor, size: 16)
                                Text(aa.name).font(Theme.bodySmall).foregroundStyle(Theme.textPrimary)
                                    .lineLimit(1).truncationMode(.tail)
                                Spacer()
                                Text(vendor.name)
                                    .font(Theme.caption).foregroundStyle(Theme.textSecondary)
                                if let intelligence = aa.intelligence {
                                    Text("\(Int(intelligence.rounded()))")
                                        .font(Theme.caption).monospacedDigit()
                                        .foregroundStyle(Theme.textSecondary)
                                }
                            }
                            .padding(.horizontal, 10).padding(.vertical, 8)
                            .contentShape(.rect)
                        }
                        .plainButton(cornerRadius: 6)
                        .modifier(RowHover())
                    }
                }
            }
            .frame(maxHeight: .infinity)
            .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(Theme.border, lineWidth: 1))
        }
        .padding(24)
        .frame(width: 460, height: 480)
        .background(Theme.panel)
    }
}

/// The loading stand-in for `results`: whichever of the chart or the
/// table `viewMode` shows, the shimmer keeps that view's footprint.
private struct ModelsSkeleton: View {
    var model: ModelsModel
    var rows = 8
    var label: String
    @State private var width: CGFloat = 0

    var body: some View {
        switch model.viewMode {
        case .chart:
            SkeletonRows(shape: .chart, label: label)
        case .table:
            table
        }
    }

    @ViewBuilder private var table: some View {
        let columns = TableColumns.widths(for: width, extraMetric: model.showsExtraMetric)
        Card {
            ScrollView(.horizontal) {
                VStack(spacing: 0) {
                    header(columns)
                    Divider().overlay(Theme.border)
                    VStack(spacing: 0) {
                        ForEach(0..<rows, id: \.self) { row in
                            tableRow(row, columns)
                            Divider().overlay(Theme.rowDivider)
                        }
                    }
                }
                .frame(width: columns.total, alignment: .leading)
            }
            .scrollIndicators(.automatic)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(label))
    }

    private func header(_ columns: TableColumns) -> some View {
        HStack(spacing: TableColumns.gap) {
            SkeletonBlock(width: 42, height: 10)
                .frame(width: columns.model, alignment: .leading)
            SkeletonBlock(width: 36, height: 10)
                .frame(width: columns.ranIn, alignment: .leading)
            SkeletonBlock(width: 13, height: 13, radius: 3)
                .frame(width: columns.intelligence, alignment: .trailing)
            SkeletonBlock(width: 13, height: 13, radius: 3)
                .frame(width: columns.coding, alignment: .trailing)
            SkeletonBlock(width: 13, height: 13, radius: 3)
                .frame(width: columns.reasoning, alignment: .trailing)
            SkeletonBlock(width: 13, height: 13, radius: 3)
                .frame(width: columns.speed, alignment: .trailing)
            if model.showsExtraMetric {
                SkeletonBlock(width: 13, height: 13, radius: 3)
                    .frame(width: columns.extra, alignment: .trailing)
            }
            SkeletonBlock(width: 56, height: 10)
                .padding(.leading, TableColumns.priceInset)
                .frame(width: columns.price, alignment: .leading)
            SkeletonBlock(width: 58, height: 10)
                .frame(width: columns.cost, alignment: .leading)
            SkeletonBlock(width: 60, height: 10)
                .frame(width: columns.tokens, alignment: .trailing)
        }
        .padding(.vertical, 6)
    }

    private func tableRow(_ row: Int, _ columns: TableColumns) -> some View {
        HStack(spacing: TableColumns.gap) {
            HStack(spacing: 8) {
                SkeletonBlock(width: 16, height: 16, radius: 4)
                SkeletonBlock(width: 110 - CGFloat(row % 3) * 22, height: 12)
            }
            .frame(width: columns.model, alignment: .leading)
            HStack(spacing: 4) {
                SkeletonBlock(width: 14, height: 14, radius: 4)
                if row % 2 == 0 { SkeletonBlock(width: 14, height: 14, radius: 4) }
            }
            .frame(width: columns.ranIn, alignment: .leading)
            SkeletonBlock(width: 24, height: 11)
                .frame(width: columns.intelligence, alignment: .trailing)
            SkeletonBlock(width: 24, height: 11)
                .frame(width: columns.coding, alignment: .trailing)
            SkeletonBlock(width: 24, height: 11)
                .frame(width: columns.reasoning, alignment: .trailing)
            SkeletonBlock(width: 26, height: 11)
                .frame(width: columns.speed, alignment: .trailing)
            if model.showsExtraMetric {
                SkeletonBlock(width: 28, height: 11)
                    .frame(width: columns.extra, alignment: .trailing)
            }
            SkeletonBlock(width: 86, height: 11)
                .padding(.leading, TableColumns.priceInset)
                .frame(width: columns.price, alignment: .leading)
            // The cost column's gradient scale: one mid-height bar.
            SkeletonBlock(width: 64, height: 8, radius: 4)
                .frame(width: columns.cost, alignment: .leading)
            SkeletonBlock(width: 40, height: 11)
                .frame(width: columns.tokens, alignment: .trailing)
        }
        .padding(.vertical, 10)
    }
}
